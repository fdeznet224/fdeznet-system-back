"""Pool de IP de administración de las antenas de radio.

Cada MikroTik puede tener una red de tipo "antenas" (ej. 13.13.13.0/24 en la
VLAN de administración) con un rango opcional para clientes (los AP, PtP y la
OLT suelen ir abajo del rango). Al activar un cliente de radio el sistema le da
la siguiente IP libre; la de un cliente dado de baja vuelve a estar libre,
salvo que el MikroTik todavía vea un equipo con ella.
"""

import ipaddress
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.services.ip_uso_service import IpUsoService
from src.infrastructure.models import ClienteModel, EquipoRedModel, OLTModel, RedModel

logger = logging.getLogger(__name__)

TIPO_ANTENAS = "antenas"
ESTADOS_DE_BAJA = ("cancelado",)
MAXIMO_DE_IPS = 1024


def _ip(valor: Any) -> Optional[ipaddress.IPv4Address]:
    try:
        ip = ipaddress.ip_address(str(valor or "").strip())
    except ValueError:
        return None
    return ip if isinstance(ip, ipaddress.IPv4Address) else None


def red_ipv4(red: RedModel) -> ipaddress.IPv4Network:
    try:
        network = ipaddress.ip_network(red.cidr, strict=False)
    except ValueError as exc:
        raise ValueError("CIDR de red inválido") from exc
    if not isinstance(network, ipaddress.IPv4Network):
        raise ValueError("La red debe ser IPv4")
    return network


def ips_del_rango(red: RedModel) -> List[ipaddress.IPv4Address]:
    """Las IP repartibles: los hosts de la red, recortados al rango si lo hay."""
    network = red_ipv4(red)
    inicio, fin = _ip(red.ip_inicio), _ip(red.ip_fin)
    ips = []
    for ip in network.hosts():
        if inicio and ip < inicio:
            continue
        if fin and ip > fin:
            break
        ips.append(ip)
        if len(ips) >= MAXIMO_DE_IPS:
            break
    return ips


def armar_mapa(
    red: RedModel,
    clientes: List[Dict[str, Any]],
    equipos: List[Dict[str, Any]],
    en_mikrotik: Dict[str, str],
) -> List[Dict[str, Any]]:
    """Cada IP del rango (y las ocupadas fuera de él) con su estado.

    estado: gateway | equipo | cliente | baja | mikrotik | libre.
    "baja" = la tenía un cliente cancelado: se puede volver a dar.
    """
    network = red_ipv4(red)
    gateway = str(_ip(red.gateway)) if _ip(red.gateway) else None
    rango = {str(ip) for ip in ips_del_rango(red)}
    por_ip: Dict[str, Dict[str, Any]] = {}

    def fila(ip: str) -> Dict[str, Any]:
        return por_ip.setdefault(ip, {"ip": ip, "estado": "libre", "en_rango": ip in rango})

    for ip in rango:
        fila(ip)
    if gateway:
        fila(gateway).update(estado="gateway", detalle="Gateway (MikroTik)")
    for equipo in equipos:
        ip = _ip(equipo.get("ip"))
        if ip and ip in network and str(ip) != gateway:
            fila(str(ip)).update(estado="equipo", detalle=equipo.get("nombre"), equipo_id=equipo.get("id"))
    # Primero los que tienen servicio: una baja nunca tapa a un cliente activo.
    ordenados = sorted(clientes, key=lambda c: c.get("estado") in ESTADOS_DE_BAJA)
    for cliente in ordenados:
        ip = _ip(cliente.get("ip_antena"))
        if not ip or ip not in network:
            continue
        actual = fila(str(ip))
        baja = cliente.get("estado") in ESTADOS_DE_BAJA
        if actual["estado"] == "libre":
            actual.update(
                estado="baja" if baja else "cliente",
                cliente_id=cliente.get("id"),
                detalle=cliente.get("nombre"),
                cliente_estado=cliente.get("estado"),
                responde=cliente.get("antena_responde"),
                revisada_en=cliente.get("antena_revisada_en"),
            )
        elif not baja:
            # Dos con la misma IP (o un cliente con la de un equipo): se avisa.
            actual.setdefault("repetida_con", []).append(cliente.get("nombre"))
    for ip_texto, origen in en_mikrotik.items():
        ip = _ip(ip_texto)
        if not ip or ip not in network:
            continue
        actual = fila(str(ip))
        actual["visto_en_mikrotik"] = origen
        if actual["estado"] == "libre":
            actual.update(estado="mikrotik", detalle=origen)
    return sorted(por_ip.values(), key=lambda f: ipaddress.ip_address(f["ip"]))


def siguiente_de_mapa(mapa: List[Dict[str, Any]], excluir: Optional[set] = None) -> Optional[str]:
    """La primera IP del rango libre o de un cliente de baja que el MikroTik ya no ve."""
    excluir = excluir or set()
    for fila in mapa:
        if not fila.get("en_rango") or fila["ip"] in excluir:
            continue
        if fila["estado"] == "libre":
            return fila["ip"]
        if fila["estado"] == "baja" and not fila.get("visto_en_mikrotik") and not fila.get("repetida_con"):
            return fila["ip"]
    return None


def resumen(mapa: List[Dict[str, Any]]) -> Dict[str, int]:
    cuenta = {"libre": 0, "cliente": 0, "baja": 0, "equipo": 0, "mikrotik": 0, "gateway": 0}
    for fila in mapa:
        cuenta[fila["estado"]] = cuenta.get(fila["estado"], 0) + 1
    cuenta["disponibles"] = sum(
        1 for f in mapa if f.get("en_rango") and (f["estado"] == "libre" or (f["estado"] == "baja" and not f.get("visto_en_mikrotik")))
    )
    return cuenta


class PoolAntenasService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def pool_de_router(self, router_id: Optional[int]) -> Optional[RedModel]:
        if not router_id:
            return None
        return (
            await self.db.execute(
                select(RedModel)
                .where(RedModel.router_id == router_id, RedModel.tipo == TIPO_ANTENAS)
                .order_by(RedModel.id)
                .limit(1)
            )
        ).scalars().first()

    async def _datos(self, red: RedModel, leer_mikrotik: bool = True) -> Dict[str, Any]:
        filas = (
            await self.db.execute(
                select(
                    ClienteModel.id, ClienteModel.nombre, ClienteModel.estado, ClienteModel.ip_antena,
                    ClienteModel.antena_responde, ClienteModel.antena_revisada_en,
                ).where(ClienteModel.ip_antena.isnot(None), ClienteModel.ip_antena != "")
            )
        ).all()
        clientes = [dict(f._mapping) for f in filas]
        equipos = [
            {"id": e.id, "nombre": e.nombre, "ip": e.ip}
            for e in (await self.db.execute(select(EquipoRedModel))).scalars().all()
        ]
        equipos += [
            {"id": None, "nombre": f"OLT {o.nombre}", "ip": o.ip}
            for o in (await self.db.execute(select(OLTModel))).scalars().all()
        ]
        en_mikrotik, error = ({}, None)
        if leer_mikrotik:
            en_mikrotik, error = await IpUsoService(self.db).ips_en_mikrotik(red)
        mapa = armar_mapa(red, clientes, equipos, en_mikrotik)
        return {"mapa": mapa, "error": error}

    async def mapa(self, red: RedModel) -> Dict[str, Any]:
        datos = await self._datos(red)
        return {
            "red": {"id": red.id, "nombre": red.nombre, "cidr": red.cidr, "gateway": red.gateway,
                    "tipo": red.tipo, "ip_inicio": red.ip_inicio, "ip_fin": red.ip_fin},
            "ips": datos["mapa"],
            "resumen": resumen(datos["mapa"]),
            "mikrotik_verificado": datos["error"] is None,
            "error": datos["error"],
        }

    async def siguiente_libre(self, red: RedModel) -> Optional[str]:
        datos = await self._datos(red)
        return siguiente_de_mapa(datos["mapa"])

    async def _quitar_a_dados_de_baja(self, ip: str, excepto_cliente_id: Optional[int] = None) -> int:
        clientes = (
            await self.db.execute(
                select(ClienteModel).where(
                    ClienteModel.ip_antena == ip, ClienteModel.estado.in_(ESTADOS_DE_BAJA),
                )
            )
        ).scalars().all()
        quitados = 0
        for cliente in clientes:
            if cliente.id != excepto_cliente_id:
                cliente.ip_antena = None
                quitados += 1
        return quitados

    async def liberar(self, red: RedModel, ip: str) -> Dict[str, Any]:
        """Quita la IP a los clientes dados de baja que la conservan."""
        ip_valida = _ip(ip)
        if not ip_valida or ip_valida not in red_ipv4(red):
            raise ValueError("Esa IP no es de esta red.")
        activo = (
            await self.db.execute(
                select(ClienteModel.nombre).where(
                    ClienteModel.ip_antena == str(ip_valida), ClienteModel.estado.notin_(ESTADOS_DE_BAJA),
                )
            )
        ).scalars().first()
        if activo:
            raise ValueError(f"La IP es de {activo}, que sigue con servicio. Cámbiala en su ficha.")
        quitados = await self._quitar_a_dados_de_baja(str(ip_valida))
        if not quitados:
            raise ValueError("Esa IP no la tiene ningún cliente: ya está libre.")
        await self.db.commit()
        return {"ip": str(ip_valida), "liberada": True, "clientes": quitados}

    async def asignar(self, router_id: Optional[int], cliente_id: Optional[int] = None) -> Optional[str]:
        """Siguiente IP del pool del MikroTik para la antena de un cliente (sin commit).

        Bloquea la fila de la red para que dos activaciones a la vez no tomen la
        misma IP. Sin pool en ese MikroTik devuelve None.
        """
        red = await self.pool_de_router(router_id)
        if not red:
            return None
        await self.db.execute(select(RedModel.id).where(RedModel.id == red.id).with_for_update())
        ip = await self.siguiente_libre(red)
        if not ip:
            raise ValueError(f"El pool de antenas {red.nombre} ya no tiene IP libres.")
        await self._quitar_a_dados_de_baja(ip, excepto_cliente_id=cliente_id)
        return ip

    async def ocupada_por_otro(self, ip: str, cliente_id: Optional[int] = None) -> Optional[str]:
        """Nombre del cliente con servicio que ya tiene esa IP de antena."""
        consulta = select(ClienteModel.nombre).where(
            ClienteModel.ip_antena == ip.strip(), ClienteModel.estado.notin_(ESTADOS_DE_BAJA),
        )
        if cliente_id:
            consulta = consulta.where(ClienteModel.id != cliente_id)
        return (await self.db.execute(consulta)).scalars().first()
