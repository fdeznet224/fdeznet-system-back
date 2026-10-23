"""La red de la casa del cliente: equipos conectados, WiFi y bloqueos.

Equipos y WiFi salen de la ONU por TR-069 (GenieACS). El bloqueo por MAC va
por la web de la ONU porque el HG8145V5 no publica su filtro MAC en TR-069.
El nombre de la red (SSID) no se cambia desde aquí: va en el contrato.
"""

import asyncio
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.application.services.acceso_onu_config import cuentas_de, marca_de_onu, obtener_acceso_onu
from src.application.services.onu_web_huawei import FiltroMacHuawei, normalizar_mac
from src.infrastructure.models import ClienteModel, LogActividadModel, ServicioModel

logger = logging.getLogger(__name__)

NBI_PREDETERMINADO = "http://127.0.0.1:7557"
LAN = "InternetGatewayDevice.LANDevice.1"
# Lo que espera el admin a que la ONU conteste el aviso; si no, se muestra lo último guardado.
ESPERA_ONU_MS = 15000
RE_CLAVE_WIFI = re.compile(r"^[\x20-\x7e]{8,63}$")


def url_nbi() -> str:
    return (os.getenv("GENIEACS_NBI_URL", "").strip() or NBI_PREDETERMINADO).rstrip("/")


def serial_tr069(identificador: str) -> Optional[str]:
    """"HWTCF1C8D69E" → "48575443F1C8D69E": la Huawei se reporta con el fabricante en hexadecimal."""
    texto = re.sub(r"[^0-9A-Za-z]", "", identificador or "").upper()
    if re.fullmatch(r"[A-Z]{4}[0-9A-F]{8}", texto):
        return texto[:4].encode("ascii").hex().upper() + texto[4:]
    return None


def _valor(nodo: Any) -> Any:
    return nodo.get("_value") if isinstance(nodo, dict) else None


def _instancias(nodo: Any) -> List[tuple]:
    """Los hijos numerados de un objeto TR-069, en orden."""
    if not isinstance(nodo, dict):
        return []
    return sorted(((int(k), v) for k, v in nodo.items() if k.isdigit() and isinstance(v, dict)), key=lambda p: p[0])


def redes_wifi(lan: Dict[str, Any]) -> List[Dict[str, Any]]:
    redes = []
    for indice, wlan in _instancias(lan.get("WLANConfiguration")):
        ssid = _valor(wlan.get("SSID"))
        if not ssid:
            continue
        clave = _valor(wlan.get("KeyPassphrase")) or _valor((wlan.get("PreSharedKey") or {}).get("1", {}).get("KeyPassphrase"))
        redes.append({
            "indice": indice,
            "ssid": ssid,
            "banda": _valor(wlan.get("X_HW_RFBand")) or "",
            "activa": str(_valor(wlan.get("Enable"))).lower() in ("true", "1"),
            "clave": clave or None,
            "macs": [normalizar_mac(_valor(d.get("AssociatedDeviceMACAddress")))
                     for _, d in _instancias(wlan.get("AssociatedDevice")) if _valor(d.get("AssociatedDeviceMACAddress"))],
        })
    return redes


def equipos(lan: Dict[str, Any], redes: List[Dict[str, Any]], bloqueadas: Optional[set]) -> List[Dict[str, Any]]:
    """Los equipos que la ONU conoce, más los bloqueados que ya no aparecen."""
    red_de = {mac: red for red in redes for mac in red["macs"]}
    red_por_indice = {red["indice"]: red for red in redes}
    lista, vistas = [], set()
    for _, host in _instancias((lan.get("Hosts") or {}).get("Host")):
        try:
            mac = normalizar_mac(_valor(host.get("MACAddress")))
        except ValueError:
            continue
        if mac in vistas:
            continue
        vistas.add(mac)
        # Layer2Interface dice en qué WiFi está aunque ya se haya desconectado.
        interfaz = re.search(r"WLANConfiguration\.(\d+)", str(_valor(host.get("Layer2Interface")) or ""))
        # Layer2Interface viene fresco con los equipos; AssociatedDevice puede ser de una lectura vieja del WiFi.
        red = (red_por_indice.get(int(interfaz.group(1))) if interfaz else None) or red_de.get(mac)
        lista.append({
            "mac": mac,
            "nombre": _valor(host.get("HostName")) or "",
            "ip": _valor(host.get("IPAddress")) or "",
            "conectado": str(_valor(host.get("Active"))).lower() in ("true", "1"),
            "conexion": "wifi" if "802.11" in str(_valor(host.get("InterfaceType"))) else "cable",
            "red": f"{red['ssid']} · {red['banda']}" if red else None,
            "indice_red": red["indice"] if red else (int(interfaz.group(1)) if interfaz else None),
            "bloqueado": mac in bloqueadas if bloqueadas is not None else None,
        })
    for mac in sorted((bloqueadas or set()) - vistas):
        lista.append({"mac": mac, "nombre": "", "ip": "", "conectado": False, "conexion": None,
                      "red": None, "indice_red": None, "bloqueado": True})
    lista.sort(key=lambda e: (not e["conectado"], not e["bloqueado"], e["nombre"].lower() or "~", e["mac"]))
    return lista


def indices_principales(redes: List[Dict[str, Any]]) -> set:
    """La red del cliente es la de la WLAN 1 (y sus otras bandas con el mismo nombre).
    Las demás, como la red de fichas "Zona FdezNet", no son suyas."""
    if not redes:
        return set()
    principal = min(redes, key=lambda r: r["indice"])["ssid"]
    return {r["indice"] for r in redes if r["ssid"] == principal}


def solo_de_la_red_principal(redes: List[Dict[str, Any]], lista: List[Dict[str, Any]]) -> tuple:
    """Lo que ve el cliente en su portal: su WiFi y los equipos que no son de otra red."""
    suyos = indices_principales(redes)
    return ([r for r in redes if r["indice"] in suyos],
            [e for e in lista if e["indice_red"] is None or e["indice_red"] in suyos])


def redes_para_bloquear(lan: Dict[str, Any], mac: str, solo_principal: bool) -> List[int]:
    redes = redes_wifi(lan)
    suyos = indices_principales(redes)
    del_equipo = next((e for e in equipos(lan, redes, None) if e["mac"] == mac), None)
    indice = del_equipo["indice_red"] if del_equipo else None
    if solo_principal and indice is not None and indice not in suyos:
        raise ValueError("Ese equipo no está en tu red.")
    if indice is None or indice in suyos:
        return sorted(suyos) or [1]
    ssid = next((r["ssid"] for r in redes if r["indice"] == indice), None)
    return sorted(r["indice"] for r in redes if r["ssid"] == ssid) or [indice]


class RedClienteService:
    def __init__(self, db: AsyncSession, nbi: Optional[str] = None):
        self.db = db
        self.nbi = nbi or url_nbi()

    # ---------------------------------------------------------- de quién es la ONU
    async def _equipo(self, cliente_id: int, servicio_id: Optional[int] = None) -> Dict[str, Any]:
        consulta = select(ServicioModel).options(selectinload(ServicioModel.onu)).where(ServicioModel.cliente_id == cliente_id)
        if servicio_id:
            consulta = consulta.where(ServicioModel.id == servicio_id)
        servicios = (await self.db.execute(consulta.order_by(ServicioModel.id))).scalars().all()
        servicio = next((s for s in servicios if s.onu), None) or next((s for s in servicios if s.ip_asignada), None)
        onu, ip = (servicio.onu, servicio.ip_asignada) if servicio else (None, None)
        if not onu or not ip:
            cliente = (await self.db.execute(
                select(ClienteModel).options(selectinload(ClienteModel.onu_asignada)).where(ClienteModel.id == cliente_id)
            )).scalars().first()
            if not cliente:
                raise LookupError("Cliente no encontrado")
            onu, ip = onu or cliente.onu_asignada, ip or cliente.ip_asignada
        if not onu and not ip:
            raise ValueError("El cliente no tiene ONU ni IP registrada.")
        return {
            "identificador": onu.identificador if onu else None,
            "modelo": onu.modelo if onu else None,
            "marca": marca_de_onu(onu.identificador if onu else None, onu.modelo if onu else None),
            "ip": ip,
        }

    # ---------------------------------------------------------- GenieACS
    async def _id_tr069(self, http: httpx.AsyncClient, equipo: Dict[str, Any]) -> Optional[str]:
        """Por el serial de la ONU; si no, por la IP a la que GenieACS le avisa."""
        consultas = []
        serie = (equipo["identificador"] or "").upper()
        if serie:
            consultas.append({"_deviceId._SerialNumber": {"$in": list(filter(None, {serie, serial_tr069(serie)}))}})
        if equipo["ip"]:
            consultas.append({"InternetGatewayDevice.ManagementServer.ConnectionRequestURL._value":
                              {"$regex": f"^https?://{re.escape(equipo['ip'])}:"}})
        for consulta in consultas:
            respuesta = await http.get("/devices", params={"query": json.dumps(consulta), "projection": "_id"})
            respuesta.raise_for_status()
            encontrados = respuesta.json()
            if encontrados:
                return encontrados[0]["_id"]
        return None

    async def _tarea(self, http: httpx.AsyncClient, device_id: str, tarea: Dict[str, Any]) -> bool:
        """True si la ONU la hizo ya; False si no contestó (queda en cola para su próximo reporte).
        Si la ONU la rechaza, se borra de la cola y se avisa."""
        respuesta = await http.post(f"/devices/{device_id}/tasks",
                                    params={"timeout": ESPERA_ONU_MS, "connection_request": ""}, json=tarea)
        if respuesta.status_code == 200:
            return True
        respuesta.raise_for_status()
        tarea_id = respuesta.json().get("_id")
        fallas = (await http.get("/faults", params={"query": json.dumps({"device": device_id})})).json()
        falla = next((f for f in fallas if f.get("channel") == f"task_{tarea_id}"), None)
        if falla:
            await http.delete(f"/tasks/{tarea_id}")
            await http.delete(f"/faults/{falla['_id']}")
            detalle = (falla.get("detail") or {}).get("faultString") or falla.get("message") or falla.get("code")
            raise ValueError(f"La ONU rechazó el cambio: {detalle}")
        return False

    async def _leer_lan(self, http: httpx.AsyncClient, device_id: str) -> Dict[str, Any]:
        respuesta = await http.get("/devices", params={
            "query": json.dumps({"_id": device_id}),
            "projection": f"_lastInform,InternetGatewayDevice.DeviceInfo.ModelName,{LAN}.Hosts,{LAN}.WLANConfiguration",
        })
        respuesta.raise_for_status()
        return (respuesta.json() or [{}])[0]

    # ---------------------------------------------------------- web de la ONU
    async def _cuentas_huawei(self) -> List[tuple]:
        config = await obtener_acceso_onu(self.db)
        return [(c["usuario"], c["clave"]) for c in cuentas_de(config["huawei"]["cuentas"])]

    async def _bloqueadas(self, equipo: Dict[str, Any]) -> set:
        if equipo["marca"] != "huawei" or not equipo["ip"]:
            raise ValueError("El bloqueo por MAC solo está hecho para ONU Huawei.")
        cuentas = await self._cuentas_huawei()
        for intento in range(2):
            try:
                async with FiltroMacHuawei(equipo["ip"], cuentas) as filtro:
                    return set(filtro.estado_inicial["bloqueadas"])
            except (ValueError, httpx.HTTPError):
                # La web de la ONU deja una sola sesión: si estaba ocupada (otro cambio), se espera y se reintenta.
                if intento:
                    raise
                await asyncio.sleep(2)

    # ---------------------------------------------------------- lo que ve el admin
    async def estado(self, cliente_id: int, servicio_id: Optional[int] = None, solo_principal: bool = False) -> Dict[str, Any]:
        equipo = await self._equipo(cliente_id, servicio_id)
        async with httpx.AsyncClient(base_url=self.nbi, timeout=ESPERA_ONU_MS / 1000 + 10) as http:
            device_id = await self._id_tr069(http, equipo)

            async def tr069():
                if not device_id:
                    return None, False
                # Solo los equipos (unos 3 s): dicen en qué red está cada uno. El WiFi tarda ~12 s en
                # contestar y sus nombres casi no cambian; se lee completo solo si no se tiene.
                al_dia = await self._tarea(http, device_id, {"name": "refreshObject", "objectName": f"{LAN}.Hosts"})
                documento = await self._leer_lan(http, device_id)
                lan = ((documento.get("InternetGatewayDevice") or {}).get("LANDevice") or {}).get("1") or {}
                if al_dia and not redes_wifi(lan):
                    await self._tarea(http, device_id, {"name": "refreshObject", "objectName": f"{LAN}.WLANConfiguration"})
                    documento = await self._leer_lan(http, device_id)
                return documento, al_dia

            async def web():
                try:
                    return await self._bloqueadas(equipo), None
                except (ValueError, httpx.HTTPError) as error:
                    return None, str(error) if isinstance(error, ValueError) else "La web de la ONU no respondió."

            (documento, al_dia), (bloqueadas, error_web) = await asyncio.gather(tr069(), web())
        lan = ((documento or {}).get("InternetGatewayDevice") or {}).get("LANDevice", {}).get("1", {})
        redes = redes_wifi(lan)
        lista = equipos(lan, redes, bloqueadas)
        if solo_principal:
            redes, lista = solo_de_la_red_principal(redes, lista)
        return {
            "ip": equipo["ip"],
            "onu": equipo["identificador"],
            "modelo": equipo["modelo"],
            "tr069": bool(device_id),
            "al_dia": al_dia,
            "ultimo_reporte": (documento or {}).get("_lastInform"),
            "equipos": [{k: v for k, v in e.items() if k != "indice_red"} for e in lista],
            "redes": [{k: v for k, v in r.items() if k != "macs"}
                      | {"conectados": sum(1 for e in lista if e["conectado"] and e["indice_red"] == r["indice"])} for r in redes],
            "bloqueo_disponible": bloqueadas is not None,
            "error_bloqueo": error_web,
        }

    async def bloquear(self, cliente_id: int, mac: str, bloquear: bool, usuario=None, servicio_id: Optional[int] = None,
                       solo_principal: bool = False) -> Dict[str, Any]:
        mac = normalizar_mac(mac)
        equipo = await self._equipo(cliente_id, servicio_id)
        if equipo["marca"] != "huawei" or not equipo["ip"]:
            raise ValueError("El bloqueo por MAC solo está hecho para ONU Huawei.")
        redes = await self._redes_del_equipo(equipo, mac, solo_principal) if bloquear else []
        cuentas = await self._cuentas_huawei()
        for intento in range(2):
            try:
                async with FiltroMacHuawei(equipo["ip"], cuentas) as filtro:
                    estado = await (filtro.bloquear(mac, redes) if bloquear else filtro.desbloquear(mac))
                break
            except (ValueError, httpx.HTTPError):
                # Sesión de la web ocupada (por ejemplo, otra lectura de la red): un reintento.
                if intento:
                    raise
                await asyncio.sleep(2)
        await self._bitacora(usuario, "Bloquear equipo" if bloquear else "Desbloquear equipo", cliente_id,
                             f"Cliente {cliente_id} · ONU {equipo['identificador'] or equipo['ip']} · {mac}"
                             + (f" · WiFi {', '.join(map(str, redes))}" if redes else ""))
        return {"bloqueados": sorted(estado["bloqueadas"])}

    async def cambiar_clave_wifi(self, cliente_id: int, ssid: str, clave: str, usuario=None,
                                 servicio_id: Optional[int] = None, solo_principal: bool = False) -> Dict[str, Any]:
        """Cambia la contraseña en todas las bandas que usan ese nombre de red."""
        if not RE_CLAVE_WIFI.match(clave or ""):
            raise ValueError("La contraseña debe tener de 8 a 63 caracteres, sin acentos ni ñ.")
        equipo = await self._equipo(cliente_id, servicio_id)
        async with httpx.AsyncClient(base_url=self.nbi, timeout=ESPERA_ONU_MS / 1000 + 10) as http:
            device_id = await self._id_tr069(http, equipo)
            if not device_id:
                raise ValueError("La ONU del cliente todavía no se reporta por TR-069.")
            lan = ((await self._leer_lan(http, device_id)).get("InternetGatewayDevice") or {}).get("LANDevice", {}).get("1", {})
            redes = redes_wifi(lan)
            indices = [r["indice"] for r in redes if r["ssid"] == ssid]
            if not indices:
                raise ValueError("Esa red WiFi no está en la ONU.")
            if solo_principal and not set(indices) <= indices_principales(redes):
                raise ValueError("Solo puedes cambiar la contraseña de tu red principal.")
            valores = []
            for i in indices:
                base = f"{LAN}.WLANConfiguration.{i}"
                valores += [[f"{base}.KeyPassphrase", clave, "xsd:string"],
                            [f"{base}.PreSharedKey.1.KeyPassphrase", clave, "xsd:string"]]
            aplicado = await self._tarea(http, device_id, {"name": "setParameterValues", "parameterValues": valores})
        await self._bitacora(usuario, "Cambiar contraseña WiFi", cliente_id,
                             f"Cliente {cliente_id} · ONU {equipo['identificador'] or equipo['ip']} · red {ssid}")
        return {"aplicado": aplicado, "bandas": len(indices)}

    async def _redes_del_equipo(self, equipo: Dict[str, Any], mac: str, solo_principal: bool) -> List[int]:
        """En qué redes WiFi bloquearlo: todas las bandas de la red donde está (2.4 y 5 GHz).
        Desde el portal solo la red principal; un equipo de otra red (la de fichas) no se toca."""
        async with httpx.AsyncClient(base_url=self.nbi, timeout=ESPERA_ONU_MS / 1000 + 10) as http:
            device_id = await self._id_tr069(http, equipo)
            if not device_id:
                return [1]
            lan = ((await self._leer_lan(http, device_id)).get("InternetGatewayDevice") or {}).get("LANDevice", {}).get("1", {})
        return redes_para_bloquear(lan, mac, solo_principal)

    async def reiniciar(self, cliente_id: int, usuario=None, servicio_id: Optional[int] = None) -> Dict[str, Any]:
        """Por TR-069 si la ONU se reporta; si no, por la OLT como en la ficha."""
        equipo = await self._equipo(cliente_id, servicio_id)
        async with httpx.AsyncClient(base_url=self.nbi, timeout=ESPERA_ONU_MS / 1000 + 10) as http:
            device_id = await self._id_tr069(http, equipo)
            if device_id:
                if await self._tarea(http, device_id, {"name": "reboot"}):
                    await self._bitacora(usuario, "Reiniciar ONU", cliente_id,
                                         f"Cliente {cliente_id} · ONU {equipo['identificador'] or equipo['ip']} · TR-069")
                    return {"via": "tr069"}
                # No contestó: se quita de la cola para que no se reinicie dos veces.
                pendientes = (await http.get("/tasks", params={"query": json.dumps({"device": device_id, "name": "reboot"})})).json()
                for tarea in pendientes:
                    await http.delete(f"/tasks/{tarea['_id']}")
        from src.application.services.vsol_api_service import VsolApiService

        await VsolApiService(self.db).reiniciar_onu_de_cliente(cliente_id, usuario)
        return {"via": "olt"}

    async def _bitacora(self, usuario, accion: str, cliente_id: int, detalle: str) -> None:
        self.db.add(LogActividadModel(
            usuario_id=getattr(usuario, "id", None), usuario_nombre=getattr(usuario, "usuario", None),
            accion=f"Red del cliente: {accion}", metodo="POST", ruta=f"/api/clientes/{cliente_id}/red",
            estado_http=200, detalle=detalle[:1000],
        ))
        await self.db.commit()
