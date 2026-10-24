"""La ONU de un cliente nuevo se configura sola al activarlo.

La ONU sale del almacén con el PPPoE de instalación (la cuenta FdezNet de cada
MikroTik), el ACS y la WAN TR069_INTERNET. Al activar al cliente queda
pendiente su configuración: en cuanto la ONU se reporta en GenieACS (se
reconoce por su serial GPON o, en EPON, por su MAC), se le pone el PPPoE del
cliente y su WiFi "FdezNet-CONTRATO" con una contraseña nueva, que se le
manda por WhatsApp junto con el acceso a su portal.

Las ZTE vienen con firmware de otra compañía y no aceptan el ACS: para ellas
el WiFi se genera igual, pero lo configura el técnico a mano (estado "manual").
"""

import json
import logging
import os
import secrets
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.application.services.acceso_onu_config import marca_de_onu
from src.application.services.onu_web_huawei import normalizar_mac
from src.application.services.red_cliente_service import (
    LAN, RedClienteService, _instancias, _valor, indices_principales, redes_wifi, serial_tr069,
)
from src.infrastructure.models import AprovisionamientoOnuModel, ClienteModel, MensajeChatModel

logger = logging.getLogger(__name__)

WAN = "InternetGatewayDevice.WANDevice.1.WANConnectionDevice"
MAC_EPON = f"{LAN}.LANHostConfigManagement.MACAddress"
# Si en un día no se reporta, ya no se espera (la ONU no trae el ACS o no se conectó).
VIGENCIA = timedelta(hours=24)
MAX_RECHAZOS = 3


def prefijo_wifi() -> str:
    return os.getenv("WIFI_PREFIJO", "FdezNet-")


def ssid_para(contrato: str) -> str:
    return f"{prefijo_wifi()}{contrato}"[:32]


def clave_wifi_nueva() -> str:
    """8 números: fácil de dictar y de escribir en un celular."""
    return f"{secrets.randbelow(10 ** 8):08d}"


def mac_epon(identificador: str) -> Optional[str]:
    try:
        return normalizar_mac(identificador).upper()
    except ValueError:
        return None


def wan_de_internet(wan: Dict[str, Any]) -> Optional[str]:
    """La WAN PPPoE del internet del cliente (TR069_INTERNET o INTERNET)."""
    candidatas = []
    for i, dispositivo in _instancias(wan):
        for j, ppp in _instancias(dispositivo.get("WANPPPConnection")):
            servicio = str(_valor(ppp.get("X_HW_SERVICELIST")) or "")
            if "INTERNET" in servicio:
                candidatas.append((servicio != "TR069_INTERNET", f"{WAN}.{i}.WANPPPConnection.{j}"))
    return min(candidatas)[1] if candidatas else None


def valores_para(documento: Dict[str, Any], ssid: str, clave_wifi: str, usuario_pppoe: str, clave_pppoe: str) -> list:
    igd = documento.get("InternetGatewayDevice") or {}
    lan = (igd.get("LANDevice") or {}).get("1") or {}
    indices = sorted(indices_principales(redes_wifi(lan)))
    if not indices:
        raise ValueError("La ONU no reporta su WiFi.")
    ppp = wan_de_internet((igd.get("WANDevice") or {}).get("1", {}).get("WANConnectionDevice") or {})
    if not ppp:
        raise ValueError("La ONU no tiene WAN PPPoE de internet.")
    valores = []
    for i in indices:
        base = f"{LAN}.WLANConfiguration.{i}"
        valores += [[f"{base}.SSID", ssid, "xsd:string"],
                    [f"{base}.KeyPassphrase", clave_wifi, "xsd:string"],
                    [f"{base}.PreSharedKey.1.KeyPassphrase", clave_wifi, "xsd:string"]]
    # El PPPoE al final: al cambiarlo la ONU se reconecta con la cuenta del cliente.
    return valores + [[f"{ppp}.Username", usuario_pppoe, "xsd:string"], [f"{ppp}.Password", clave_pppoe, "xsd:string"]]


class AprovisionamientoOnuService:
    def __init__(self, db: AsyncSession, nbi: Optional[str] = None, enviar_whatsapp=None, transporte=None):
        self.db = db
        self.transporte = transporte
        self.red = RedClienteService(db, nbi)
        self.enviar_whatsapp = enviar_whatsapp or self._whatsapp

    async def programar(self, cliente_id: int) -> Optional[Dict[str, Any]]:
        """Deja lista la configuración de la ONU del cliente; None si no aplica (sin ONU, sin PPPoE).
        `automatica` dice si se configura sola o si el técnico pone el WiFi a mano (ZTE)."""
        cliente = (await self.db.execute(
            select(ClienteModel).options(selectinload(ClienteModel.onu_asignada)).where(ClienteModel.id == cliente_id)
        )).scalars().first()
        identificador = (cliente.onu_asignada.identificador or "").strip() if cliente and cliente.onu_asignada else ""
        if not cliente or not cliente.cedula or not cliente.user_pppoe or not cliente.pass_pppoe:
            return None
        if not (serial_tr069(identificador) or mac_epon(identificador)):
            return None
        automatica = marca_de_onu(identificador, cliente.onu_asignada.modelo) != "zte"
        anteriores = (await self.db.execute(
            select(AprovisionamientoOnuModel).where(AprovisionamientoOnuModel.cliente_id == cliente_id,
                                                    AprovisionamientoOnuModel.estado == "pendiente")
        )).scalars().all()
        for anterior in anteriores:
            await self.db.delete(anterior)
        registro = AprovisionamientoOnuModel(cliente_id=cliente_id, identificador=identificador,
                                             ssid=ssid_para(cliente.cedula), clave_wifi=clave_wifi_nueva(),
                                             estado="pendiente" if automatica else "manual")
        self.db.add(registro)
        await self.db.commit()
        return {"red": registro.ssid, "clave": registro.clave_wifi, "automatica": automatica}

    async def del_cliente(self, cliente_id: int) -> Optional[Dict[str, Any]]:
        registro = (await self.db.execute(
            select(AprovisionamientoOnuModel).where(AprovisionamientoOnuModel.cliente_id == cliente_id)
            .order_by(AprovisionamientoOnuModel.id.desc()).limit(1)
        )).scalars().first()
        if not registro:
            return None
        return {"estado": registro.estado, "red": registro.ssid, "clave": registro.clave_wifi, "detalle": registro.detalle,
                "creado_en": registro.creado_en, "aplicado_en": registro.aplicado_en}

    async def reintentar(self, cliente_id: int) -> Optional[Dict[str, Any]]:
        """Vuelve a esperar a la ONU (por ejemplo, ya se le puso el ACS) con la misma red y contraseña."""
        registro = (await self.db.execute(
            select(AprovisionamientoOnuModel).where(AprovisionamientoOnuModel.cliente_id == cliente_id)
            .order_by(AprovisionamientoOnuModel.id.desc()).limit(1)
        )).scalars().first()
        if not registro:
            raise ValueError("Este cliente no tiene configuración automática de su ONU.")
        registro.estado, registro.intentos, registro.detalle, registro.creado_en = "pendiente", 0, None, datetime.now()
        await self.db.commit()
        return await self.del_cliente(cliente_id)

    async def procesar(self) -> Dict[str, int]:
        pendientes = (await self.db.execute(
            select(AprovisionamientoOnuModel).where(AprovisionamientoOnuModel.estado == "pendiente")
        )).scalars().all()
        conteo = {"pendientes": len(pendientes), "aplicadas": 0}
        if not pendientes:
            return conteo
        async with httpx.AsyncClient(base_url=self.red.nbi, timeout=40, transport=self.transporte) as http:
            for registro in pendientes:
                try:
                    if await self._procesar_uno(http, registro):
                        conteo["aplicadas"] += 1
                except httpx.HTTPError as error:
                    logger.warning("GenieACS no respondió al configurar la ONU del cliente %s: %s", registro.cliente_id, error)
                await self.db.commit()
        return conteo

    async def _procesar_uno(self, http: httpx.AsyncClient, registro: AprovisionamientoOnuModel) -> bool:
        if datetime.now() - registro.creado_en > VIGENCIA:
            registro.estado = "vencido"
            registro.detalle = "La ONU no se reportó en GenieACS: revisa que traiga el ACS y la WAN TR069_INTERNET."
            return False
        device_id = await self._buscar(http, registro)
        if not device_id:
            return False
        cliente = await self.db.get(ClienteModel, registro.cliente_id)
        # Si GenieACS no le pudo avisar, lo de la vuelta anterior sigue en cola: se espera su próximo reporte.
        en_cola = await http.get("/tasks", params={"query": json.dumps({"device": device_id}), "projection": "_id"})
        en_cola.raise_for_status()
        if en_cola.json():
            return False
        try:
            al_dia = all([
                await self.red._tarea(http, device_id, {"name": "refreshObject", "objectName": f"{LAN}.WLANConfiguration"}),
                await self.red._tarea(http, device_id, {"name": "refreshObject", "objectName": WAN}),
            ])
            if not al_dia:
                return False  # No contestó al aviso: se intenta en la siguiente vuelta.
            documento = await self._leer(http, device_id)
            valores = valores_para(documento, registro.ssid, registro.clave_wifi, cliente.user_pppoe, cliente.pass_pppoe)
            aplicado = await self.red._tarea(http, device_id, {"name": "setParameterValues", "parameterValues": valores})
        except ValueError as error:
            registro.intentos += 1
            registro.detalle = str(error)[:255]
            if registro.intentos >= MAX_RECHAZOS:
                registro.estado = "error"
            return False
        registro.estado, registro.device_id, registro.aplicado_en = "aplicado", device_id, datetime.now()
        registro.detalle = None if aplicado else "Enviado; la ONU lo aplica en su próximo reporte."
        await self._avisar(cliente, registro)
        return True

    async def _buscar(self, http: httpx.AsyncClient, registro: AprovisionamientoOnuModel) -> Optional[str]:
        serial = serial_tr069(registro.identificador)
        if serial:
            return await self._primero(http, {"_deviceId._SerialNumber": serial})
        mac = mac_epon(registro.identificador)
        if not mac:
            return None
        encontrado = await self._primero(http, {f"{MAC_EPON}._value": {"$regex": f"^{mac}$", "$options": "i"}})
        if encontrado:
            return encontrado
        # EPON nueva: GenieACS aún no tiene su MAC; se le pregunta a las que se registraron desde el alta.
        desde = registro.creado_en.timestamp() - 2 * 3600
        respuesta = await http.get("/devices", params={"projection": f"_id,_registered,{MAC_EPON}"})
        respuesta.raise_for_status()
        for fila in respuesta.json():
            registrada = fila.get("_registered")
            if not registrada or _iso(registrada) < desde or _ruta(fila, MAC_EPON):
                continue
            try:
                await self.red._tarea(http, fila["_id"], {"name": "getParameterValues", "parameterNames": [MAC_EPON]})
            except ValueError:
                continue
            if await self._primero(http, {"_id": fila["_id"], f"{MAC_EPON}._value": {"$regex": f"^{mac}$", "$options": "i"}}):
                return fila["_id"]
        return None

    async def _primero(self, http: httpx.AsyncClient, consulta: dict) -> Optional[str]:
        respuesta = await http.get("/devices", params={"query": json.dumps(consulta), "projection": "_id"})
        respuesta.raise_for_status()
        filas = respuesta.json()
        return filas[0]["_id"] if filas else None

    async def _leer(self, http: httpx.AsyncClient, device_id: str) -> Dict[str, Any]:
        respuesta = await http.get("/devices", params={
            "query": json.dumps({"_id": device_id}), "projection": f"{LAN}.WLANConfiguration,{WAN}",
        })
        respuesta.raise_for_status()
        return (respuesta.json() or [{}])[0]

    async def _avisar(self, cliente: ClienteModel, registro: AprovisionamientoOnuModel) -> None:
        if not cliente.telefono:
            return
        portal = (os.getenv("PUBLIC_URL", "").strip().rstrip("/") + "/cliente") if os.getenv("PUBLIC_URL") else None
        texto = (
            f"¡Bienvenido, {cliente.nombre.split()[0] if cliente.nombre else ''}! Tu internet ya está listo.\n\n"
            f"📶 Red WiFi: *{registro.ssid}*\n🔑 Contraseña: *{registro.clave_wifi}*\n"
            f"📄 Tu contrato: *{cliente.cedula}*"
            + (f"\n\nEn tu portal puedes ver tus pagos, cambiar la contraseña del WiFi y controlar tus equipos: {portal}\n"
               f"Entra con tu contrato; la primera vez la contraseña también es tu contrato." if portal else "")
        )
        try:
            await self.enviar_whatsapp(cliente, texto)
        except Exception:
            logger.exception("No se pudo mandar el WiFi al cliente %s", cliente.id)

    async def _whatsapp(self, cliente: ClienteModel, texto: str) -> None:
        from src.infrastructure.whatsapp_client import whatsapp_queue

        mensaje = MensajeChatModel(cliente_id=cliente.id, telefono=cliente.telefono, direccion="salida", tipo_mensaje="texto",
                                   tipo_evento="wifi_instalacion", leido=True, ack=0, estado_envio="pendiente", mensaje=texto)
        self.db.add(mensaje)
        await self.db.commit()
        await self.db.refresh(mensaje)
        await whatsapp_queue.agregar_tarea({"mensaje_chat_id": mensaje.id, "intervalo": 0, "tipo_evento": "wifi_instalacion"})


def _iso(valor: str) -> float:
    return datetime.fromisoformat(valor.replace("Z", "+00:00")).timestamp()


def _ruta(documento: Dict[str, Any], ruta: str) -> Any:
    nodo: Any = documento
    for parte in ruta.split("."):
        nodo = (nodo or {}).get(parte) if isinstance(nodo, dict) else None
    return _valor(nodo) if isinstance(nodo, dict) else None
