"""Abrir desde el admin la página del equipo del cliente.

ONU y router de radio: el servidor alcanza la IP del cliente por la VPN, así
que prueba primero el puerto de su marca (Huawei 80, ZTE 8090, router de radio
8080) y, si no contesta, los demás puertos configurados.

Antena Ubiquiti del radioenlace: va en una VLAN que sólo ve su MikroTik. Se
abre un puente temporal en el MikroTik (un puerto de su IP de la VPN que lleva
a la antena) y se cierra solo a los PUENTE_MINUTOS.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.application.services.acceso_onu_config import marca_de_onu, obtener_acceso_onu
from src.infrastructure.mikrotik_service import MikroTikService
from src.infrastructure.models import ClienteModel, RouterModel, ServicioModel

logger = logging.getLogger(__name__)

ESPERA_PUERTO_SEGUNDOS = 2.0
PUERTO_WEB = "80"
PUENTE_MINUTOS = 30
PUENTE_PUERTO_BASE = 18000
PUENTE_COMENTARIO = "FDEZNET-ANTENA"
RUTAS_PUENTE = ("/ip/firewall/nat", "/ip/firewall/filter")
FORMATO_EXPIRA = "%Y-%m-%dT%H:%M"


def url_de(ip: str, puerto: str, https: bool = False) -> str:
    esquema = "https" if https or puerto == "443" else "http"
    omitido = {"http": "80", "https": "443"}[esquema]
    return f"{esquema}://{ip}" + ("" if puerto in ("", omitido) else f":{puerto}")


def puertos_a_probar(config: dict, marca: Optional[str], radio: bool) -> List[str]:
    """El de su equipo primero; luego los demás que tiene configurados el ISP."""
    propio = (config["radio"]["puerto"] or PUERTO_WEB) if radio else (
        config[marca]["puerto"] if marca else PUERTO_WEB
    )
    orden = [propio, PUERTO_WEB, config["huawei"]["puerto"], config["zte"]["puerto"], config["radio"]["puerto"]]
    return list(dict.fromkeys(p for p in orden if p))


async def _responde(ip: str, puerto: str) -> bool:
    try:
        _, escritor = await asyncio.wait_for(asyncio.open_connection(ip, int(puerto)), ESPERA_PUERTO_SEGUNDOS)
    except (OSError, asyncio.TimeoutError, ValueError):
        return False
    escritor.close()
    try:
        await escritor.wait_closed()
    except OSError:
        pass
    return True


async def primer_puerto_que_responde(ip: str, puertos: List[str]) -> Optional[str]:
    """Se prueban todos a la vez; gana el primero de la lista que contestó."""
    respuestas = await asyncio.gather(*(_responde(ip, p) for p in puertos))
    return next((p for p, ok in zip(puertos, respuestas) if ok), None)


def comentario_puente(cliente_id: int, expira: datetime) -> str:
    return f"{PUENTE_COMENTARIO} {cliente_id} {expira.strftime(FORMATO_EXPIRA)}"


def puente_vencido(comentario: str, ahora: datetime) -> bool:
    partes = str(comentario or "").split()
    if len(partes) != 3 or partes[0] != PUENTE_COMENTARIO:
        return False
    try:
        return datetime.strptime(partes[2], FORMATO_EXPIRA) <= ahora
    except ValueError:
        return True


def _del_cliente(comentario: str, cliente_id: int) -> bool:
    partes = str(comentario or "").split()
    return len(partes) == 3 and partes[0] == PUENTE_COMENTARIO and partes[1] == str(cliente_id)


def reglas_puente(ip_vpn: str, puerto_puente: int, ip_antena: str, puerto_antena: str, comentario: str) -> Dict[str, dict]:
    """dst-nat del puerto del MikroTik a la antena; masquerade para que la
    antena conteste al MikroTik aunque no tenga ruta hacia la VPN; y un accept
    en forward por si el firewall del router bloquea lo que viene de la VPN."""
    return {
        "dstnat": {
            "chain": "dstnat", "protocol": "tcp", "dst-address": ip_vpn, "dst-port": str(puerto_puente),
            "action": "dst-nat", "to-addresses": ip_antena, "to-ports": puerto_antena, "comment": comentario,
        },
        "srcnat": {
            "chain": "srcnat", "protocol": "tcp", "dst-address": ip_antena, "dst-port": puerto_antena,
            "action": "masquerade", "comment": comentario,
        },
        "forward": {
            "chain": "forward", "protocol": "tcp", "dst-address": ip_antena, "dst-port": puerto_antena,
            "connection-nat-state": "dstnat", "action": "accept", "comment": comentario,
        },
    }


class AccesoRemotoService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def equipo_por_ip(self, ip: str) -> Dict:
        """La IP debe ser de un cliente: no se prueban puertos de cualquier dirección."""
        ip = (ip or "").strip()
        servicio = (
            await self.db.execute(
                select(ServicioModel)
                .options(selectinload(ServicioModel.onu), selectinload(ServicioModel.cliente))
                .where(ServicioModel.ip_asignada == ip)
                .limit(1)
            )
        ).scalars().first()
        cliente = servicio.cliente if servicio else (
            await self.db.execute(select(ClienteModel).where(ClienteModel.ip_asignada == ip).limit(1))
        ).scalars().first()
        if not cliente:
            raise ValueError("Esa IP no es de ningún cliente.")
        onu = servicio.onu if servicio and servicio.onu else None
        radio = getattr(cliente, "tipo_conexion", "fibra") == "radio"
        marca = None if radio or not onu else marca_de_onu(onu.identificador, onu.modelo)
        puertos = puertos_a_probar(await obtener_acceso_onu(self.db), marca, radio)
        puerto = await primer_puerto_que_responde(ip, puertos)
        return {
            "ip": ip,
            "marca": "radio" if radio else marca,
            "puerto": puerto or puertos[0],
            "responde": puerto is not None,
            "probados": puertos,
            "url": url_de(ip, puerto or puertos[0]),
        }

    async def _router(self, cliente: ClienteModel) -> RouterModel:
        router = await self.db.get(RouterModel, cliente.router_id) if cliente.router_id else None
        if not router:
            raise ValueError("El cliente no tiene MikroTik asignado.")
        return router

    async def abrir_antena(self, cliente_id: int) -> Dict:
        cliente = await self.db.get(ClienteModel, cliente_id)
        if not cliente:
            raise ValueError("Cliente no encontrado.")
        if cliente.tipo_conexion != "radio" or not cliente.ip_antena:
            raise ValueError("Falta la IP de la antena en la ficha del cliente.")
        router = await self._router(cliente)
        puerto_antena = (await obtener_acceso_onu(self.db))["antena"]["puerto"]
        puerto_puente = PUENTE_PUERTO_BASE + cliente.id % 10000
        expira = datetime.now() + timedelta(minutes=PUENTE_MINUTOS)
        reglas = reglas_puente(
            router.ip_vpn, puerto_puente, cliente.ip_antena.strip(), puerto_antena, comentario_puente(cliente.id, expira)
        )
        mk = MikroTikService(router.ip_vpn, router.user_api, router.pass_api, router.port_api)
        await asyncio.to_thread(self._crear_puente, mk, cliente.id, reglas)
        return {
            "ip": cliente.ip_antena,
            "router": router.nombre,
            "puerto": puerto_puente,
            "url": url_de(router.ip_vpn, str(puerto_puente), https=puerto_antena == "443"),
            "expira": expira.isoformat(timespec="minutes"),
        }

    @staticmethod
    def _crear_puente(mk: MikroTikService, cliente_id: int, reglas: Dict[str, dict]) -> None:
        # Uno por cliente: si ya había uno se reemplaza y se renueva el tiempo.
        for ruta in RUTAS_PUENTE:
            for regla in mk._request("GET", ruta, raise_on_error=True) or []:
                if _del_cliente(regla.get("comment"), cliente_id):
                    mk._request("DELETE", f"{ruta}/{regla['.id']}", raise_on_error=True)
        mk._request("PUT", "/ip/firewall/nat", reglas["dstnat"], raise_on_error=True)
        mk._request("PUT", "/ip/firewall/nat", reglas["srcnat"], raise_on_error=True)
        creada = mk._request("PUT", "/ip/firewall/filter", reglas["forward"], raise_on_error=True)
        if isinstance(creada, dict) and creada.get(".id"):
            mk._mover_al_inicio("/ip/firewall/filter", "forward", [creada[".id"]])

    @staticmethod
    def _cerrar_vencidos(mk: MikroTikService, ahora: datetime) -> int:
        cerradas = 0
        for ruta in RUTAS_PUENTE:
            for regla in mk._request("GET", ruta, raise_on_error=True) or []:
                if puente_vencido(regla.get("comment"), ahora):
                    mk._request("DELETE", f"{ruta}/{regla['.id']}", raise_on_error=True)
                    cerradas += 1
        return cerradas

    async def cerrar_puentes_vencidos(self) -> int:
        """Pasada automática (con la revisión de antenas): borra los puentes vencidos."""
        routers = (
            await self.db.execute(
                select(RouterModel)
                .join(ClienteModel, ClienteModel.router_id == RouterModel.id)
                .where(ClienteModel.tipo_conexion == "radio", or_(RouterModel.is_active.is_(True), RouterModel.is_active.is_(None)))
                .distinct()
            )
        ).scalars().all()
        ahora, cerradas = datetime.now(), 0
        for router in routers:
            mk = MikroTikService(router.ip_vpn, router.user_api, router.pass_api, router.port_api)
            try:
                cerradas += await asyncio.to_thread(self._cerrar_vencidos, mk, ahora)
            except Exception as exc:
                logger.warning("No se pudieron cerrar los puentes a antenas en %s: %s", router.nombre, exc)
        return cerradas
