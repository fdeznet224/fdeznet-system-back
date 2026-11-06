"""La red de la casa del cliente: equipos conectados, WiFi y bloqueos.

Equipos y WiFi salen de la ONU por TR-069 (GenieACS). En la Huawei el bloqueo
por MAC va por su web porque el HG8145V5 no publica su filtro MAC en TR-069;
la ZTE sí lo publica y va por TR-069: el filtro MAC de su firewall (deja al equipo
conectado y sin internet, como la Huawei). La lista de bloqueo del WiFi lo rechaza al
conectarse y algunos celulares (Samsung) estrenan otra MAC aleatoria y vuelven a entrar;
esa lista solo se usa para equipos de la red de fichas, que va en puente y el firewall no ve.
El nombre de la red (SSID) no se cambia desde aquí: va en el contrato.
"""

import asyncio
import json
import logging
import os
import re
from urllib.parse import quote
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.application.services.acceso_onu_config import cuentas_de, marca_de_onu, modelo_real, obtener_acceso_onu
from src.application.services.onu_web_huawei import FiltroMacHuawei, normalizar_mac
from src.infrastructure.models import (
    AprovisionamientoOnuModel, ClienteModel, EquipoNombreModel, LogActividadModel, ServicioModel,
)

logger = logging.getLogger(__name__)

NBI_PREDETERMINADO = "http://127.0.0.1:7557"
LAN = "InternetGatewayDevice.LANDevice.1"
FIREWALL = "InternetGatewayDevice.Firewall"
# Lo que espera el admin a que la ONU conteste el aviso; si no, se muestra lo último guardado.
ESPERA_ONU_MS = 15000
RE_CLAVE_WIFI = re.compile(r"^[\x20-\x7e]{8,63}$")
# Dónde va la contraseña del WiFi: la Huawei V5R020 rechaza KeyPassphrase y solo acepta
# PreSharedKey.1.KeyPassphrase; si una ONU rechaza el primero, se prueba el otro.
CAMPOS_CLAVE_WIFI = ("PreSharedKey.1.KeyPassphrase", "KeyPassphrase")
MAX_APODO = 40
# Los parámetros propios de la ZTE llevan el prefijo de su firmware: X_ZTE-COM_ (el de
# fábrica) o X_TELMEX_COM_ (el de Telmex). La misma ONU pasa de uno a otro si le cambian
# el firmware, así que se averigua en cada lectura y nunca se guarda por modelo.
PREFIJOS_ZTE = ("X_ZTE-COM", "X_TELMEX_COM")
MAC_VACIA = "00:00:00:00:00:00"
# Una sola sesión a la vez con cada ONU: si el bloqueo y la confirmación del portal le hablan
# juntos, la ZTE no contesta el segundo aviso y GenieACS la da por desconectada.
_CANDADOS: Dict[str, asyncio.Lock] = {}


def _candado(device_id: str) -> asyncio.Lock:
    return _CANDADOS.setdefault(device_id, asyncio.Lock())


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


def prefijo_zte(nodo: Any, profundidad: int = 8) -> Optional[str]:
    """"X_ZTE-COM" o "X_TELMEX_COM" según los parámetros que trae la ONU; None si no es ZTE."""
    if not isinstance(nodo, dict) or profundidad < 0:
        return None
    for clave in nodo:
        for prefijo in PREFIJOS_ZTE:
            if clave.startswith(prefijo + "_"):
                return prefijo
    for clave, hijo in nodo.items():
        if not clave.startswith("_"):
            encontrado = prefijo_zte(hijo, profundidad - 1)
            if encontrado:
                return encontrado
    return None


def _mac_o_none(valor: Any) -> Optional[str]:
    try:
        mac = normalizar_mac(str(valor or ""))
    except ValueError:
        return None
    return None if mac == MAC_VACIA else mac


def _banda(wlan: Dict[str, Any]) -> str:
    """Huawei: X_HW_RFBand; ZTE: <prefijo>_OperatingFrequencyBand."""
    for campo in ["X_HW_RFBand"] + [f"{p}_OperatingFrequencyBand" for p in PREFIJOS_ZTE]:
        if _valor(wlan.get(campo)):
            return str(_valor(wlan.get(campo)))
    return ""


def filtro_mac_zte(firewall: Dict[str, Any]) -> tuple:
    """(prefijo, nodo <prefijo>_MacFilterService) del firewall de la ZTE; (None, None) si no lo trae."""
    for prefijo in PREFIJOS_ZTE:
        nodo = (firewall or {}).get(f"{prefijo}_MacFilterService")
        if isinstance(nodo, dict):
            return prefijo, nodo
    return None, None


def bloqueadas_zte(lan: Dict[str, Any], firewall: Optional[Dict[str, Any]] = None) -> set:
    """Las MAC del filtro MAC del firewall (encendido en lista negra) y las de la lista de
    bloqueo (modo Ban) de cada WiFi de la ZTE."""
    macs = set()
    _, filtro = filtro_mac_zte(firewall or {})
    if filtro and _valor(filtro.get("Enable")) in (True, "true", "1", 1) \
            and str(_valor(filtro.get("Mode")) or "") == "Black List":
        for _, regla in _instancias(filtro.get("Filter")):
            mac = _mac_o_none(_valor(regla.get("SourceMACAddress")))
            if mac:
                macs.add(mac)
    wlans = lan.get("WLANConfiguration")
    prefijo = prefijo_zte(wlans)
    if not prefijo:
        return macs
    for _, wlan in _instancias(wlans):
        if str(_valor(wlan.get(f"{prefijo}_AccessControlMode")) or "") != "Ban":
            continue
        for _, entrada in _instancias(wlan.get(f"{prefijo}_AccessControl")):
            mac = _mac_o_none(_valor(entrada.get("MACAddress")))
            if mac:
                macs.add(mac)
    return macs


def modelo_tr069(documento: Optional[Dict[str, Any]], marca: Optional[str]) -> Optional[str]:
    """"ZTE F670L" con el ModelName que reporta la ONU; None si no lo trae."""
    info = ((documento or {}).get("InternetGatewayDevice") or {}).get("DeviceInfo") or {}
    modelo = str(_valor(info.get("ModelName")) or "").strip()
    if not modelo:
        return None
    nombre = {"zte": "ZTE", "huawei": "Huawei"}.get(marca or "")
    return f"{nombre} {modelo}" if nombre and not modelo.lower().startswith(nombre.lower()) else modelo


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
            "banda": _banda(wlan),
            "activa": str(_valor(wlan.get("Enable"))).lower() in ("true", "1"),
            "clave": clave or None,
            "macs": [normalizar_mac(_valor(d.get("AssociatedDeviceMACAddress")))
                     for _, d in _instancias(wlan.get("AssociatedDevice")) if _valor(d.get("AssociatedDeviceMACAddress"))],
        })
    return redes


def equipos(lan: Dict[str, Any], redes: List[Dict[str, Any]], bloqueadas: Optional[set],
            apodos: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """Los equipos que la ONU conoce, más los bloqueados que ya no aparecen.

    `apodo` es el nombre que les puso el cliente ("Celular de Juan"); se guarda por MAC.
    """
    apodos = apodos or {}
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
            "apodo": apodos.get(mac),
        })
    for mac in sorted((bloqueadas or set()) - vistas):
        lista.append({"mac": mac, "nombre": "", "ip": "", "conectado": False, "conexion": None,
                      "red": None, "indice_red": None, "bloqueado": True, "apodo": apodos.get(mac)})
    lista.sort(key=lambda e: (not e["conectado"], not e["bloqueado"], (e["apodo"] or e["nombre"]).lower() or "~", e["mac"]))
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
            "modelo": modelo_real(onu.modelo) if onu else None,
            "marca": marca_de_onu(onu.identificador if onu else None, onu.modelo if onu else None),
            "ip": ip,
            "_onu": onu,
        }

    async def _marca_por_tr069(self, http: httpx.AsyncClient, device_id: Optional[str], equipo: Dict[str, Any]) -> None:
        """Una ONU EPON se registra con su MAC, que no dice la marca: se le pregunta a ella.
        Si es Huawei (o ZTE), su modelo queda guardado en el inventario para la próxima vez; también
        reemplaza el "Registrada por el técnico" que queda cuando la dio de alta sin modelo."""
        if (equipo["marca"] and equipo["modelo"]) or not device_id:
            return
        respuesta = await http.get("/devices", params={
            "query": json.dumps({"_id": device_id}),
            "projection": "_deviceId,InternetGatewayDevice.DeviceInfo.Manufacturer,InternetGatewayDevice.DeviceInfo.ModelName",
        })
        respuesta.raise_for_status()
        documento = (respuesta.json() or [{}])[0]
        # _deviceId llega en cada reporte de la ONU; DeviceInfo solo si alguien lo pidió antes.
        ident = documento.get("_deviceId") or {}
        info = ((documento.get("InternetGatewayDevice") or {}).get("DeviceInfo") or {})
        fabricante = str(ident.get("_Manufacturer") or _valor(info.get("Manufacturer")) or "").strip()
        modelo = str(ident.get("_ProductClass") or _valor(info.get("ModelName")) or "").strip()
        marca = marca_de_onu(None, f"{fabricante} {modelo}")
        if not marca:
            return
        equipo["marca"] = equipo["marca"] or marca
        onu = equipo.get("_onu")
        if onu is not None and not modelo_real(onu.modelo) and modelo:
            nombre = "Huawei" if marca == "huawei" else "ZTE"
            onu.modelo = (modelo if modelo.lower().startswith(nombre.lower()) else f"{nombre} {modelo}")[:100]
            equipo["modelo"] = onu.modelo
            await self.db.commit()

    async def _equipo_con_marca(self, cliente_id: int, servicio_id: Optional[int] = None) -> Dict[str, Any]:
        equipo = await self._equipo(cliente_id, servicio_id)
        if not equipo["marca"]:
            async with httpx.AsyncClient(base_url=self.nbi, timeout=ESPERA_ONU_MS / 1000 + 10) as http:
                await self._marca_por_tr069(http, await self._id_tr069(http, equipo), equipo)
        return equipo

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
        # El id puede traer "%2D" (la HG8145X6-10 lo reporta así): va codificado otra vez en la ruta
        # o GenieACS lo lee como "-" y contesta 404.
        respuesta = await http.post(f"/devices/{quote(device_id, safe='')}/tasks",
                                    params={"timeout": ESPERA_ONU_MS, "connection_request": ""}, json=tarea)
        if respuesta.status_code == 200:
            return True
        respuesta.raise_for_status()
        tarea_id = respuesta.json().get("_id")
        fallas = (await http.get("/faults", params={"query": json.dumps({"device": device_id})})).json()
        falla = next((f for f in fallas if f.get("channel") == f"task_{tarea_id}"), None)
        if falla:
            await http.delete(f"/tasks/{tarea_id}")
            await http.delete(f"/faults/{quote(falla['_id'], safe='')}")
            if falla.get("code") == "session_terminated":
                # La ZTE aplica el cambio de PPPoE y corta la sesión sin contestar (el ACS va por
                # esa misma WAN): no es un rechazo, es "enviado" y se ve en su próximo reporte.
                return False
            detalle = (falla.get("detail") or {}).get("faultString") or falla.get("message") or falla.get("code")
            raise ValueError(f"La ONU rechazó el cambio: {detalle}")
        return False

    async def _leer_lan(self, http: httpx.AsyncClient, device_id: str) -> Dict[str, Any]:
        respuesta = await http.get("/devices", params={
            "query": json.dumps({"_id": device_id}),
            "projection": f"_lastInform,InternetGatewayDevice.DeviceInfo.ModelName,{LAN}.Hosts,{LAN}.WLANConfiguration,"
                          + ",".join(f"{FIREWALL}.{p}_MacFilterService" for p in PREFIJOS_ZTE),
        })
        respuesta.raise_for_status()
        return (respuesta.json() or [{}])[0]

    # ---------------------------------------------------------- web de la ONU
    async def _cuentas_huawei(self) -> List[tuple]:
        config = await obtener_acceso_onu(self.db)
        return [(c["usuario"], c["clave"]) for c in cuentas_de(config["huawei"]["cuentas"])]

    async def _lan(self, http: httpx.AsyncClient, device_id: str) -> Dict[str, Any]:
        return ((await self._leer_lan(http, device_id)).get("InternetGatewayDevice") or {}).get("LANDevice", {}).get("1", {})

    async def _obligada(self, http: httpx.AsyncClient, device_id: str, tarea: Dict[str, Any]) -> None:
        """Una orden que tiene que hacerse ya; si la ONU no contesta se quita de la cola y se avisa."""
        if await self._tarea(http, device_id, tarea):
            return
        pendientes = (await http.get("/tasks", params={"query": json.dumps({"device": device_id})})).json()
        for pendiente in pendientes:
            await http.delete(f"/tasks/{pendiente['_id']}")
        raise ValueError("La ONU no contestó; intenta de nuevo en un momento.")

    async def agregar_instancia(self, http: httpx.AsyncClient, device_id: str, objeto: str) -> int:
        """addObject y el número de la instancia nueva (GenieACS no lo devuelve: se compara antes y después)."""
        async def instancias() -> set:
            respuesta = await http.get("/devices", params={"query": json.dumps({"_id": device_id}), "projection": objeto})
            respuesta.raise_for_status()
            nodo = (respuesta.json() or [{}])[0]
            for parte in objeto.split("."):
                nodo = (nodo or {}).get(parte) or {}
            return {int(k) for k in nodo if k.isdigit()}

        antes = await instancias()
        await self._obligada(http, device_id, {"name": "addObject", "objectName": objeto})
        nuevas = await instancias() - antes
        if not nuevas:
            # GenieACS suele anotar la instancia nueva al crearla; si no, se le pregunta a la ONU.
            await self._obligada(http, device_id, {"name": "refreshObject", "objectName": objeto})
            nuevas = await instancias() - antes
        if not nuevas:
            raise ValueError(f"La ONU no creó {objeto.rsplit('.', 1)[-1]}.")
        return max(nuevas)

    async def _documento_zte(self, http: httpx.AsyncClient, device_id: str, wifi: bool = False) -> tuple:
        """(lan, firewall) de la ONU con su filtro MAC recién leído (unos 2 s). El WiFi tarda
        ~12 s en contestar: se relee solo con `wifi` o si no se tiene; si no, va lo guardado."""
        def partes(documento):
            igd = documento.get("InternetGatewayDevice") or {}
            return (igd.get("LANDevice") or {}).get("1") or {}, igd.get("Firewall") or {}

        lan, firewall = partes(await self._leer_lan(http, device_id))
        if wifi or not redes_wifi(lan):
            await self._obligada(http, device_id, {"name": "refreshObject", "objectName": f"{LAN}.WLANConfiguration"})
        prefijo = filtro_mac_zte(firewall)[0] or prefijo_zte(lan.get("WLANConfiguration"))
        await self._obligada(http, device_id, {"name": "refreshObject",
                                               "objectName": f"{FIREWALL}.{prefijo}_MacFilterService" if prefijo else FIREWALL})
        return partes(await self._leer_lan(http, device_id))

    async def _bloqueadas_zte(self, equipo: Dict[str, Any]) -> set:
        async with httpx.AsyncClient(base_url=self.nbi, timeout=ESPERA_ONU_MS / 1000 + 10) as http:
            device_id = await self._id_tr069(http, equipo)
            if not device_id:
                raise ValueError("La ONU del cliente todavía no se reporta por TR-069.")
            # Si hay un bloqueo en curso en esta ONU se espera a que termine: dos sesiones a la
            # vez con la ZTE hacen que GenieACS la dé por desconectada.
            async with _candado(device_id):
                return bloqueadas_zte(*await self._documento_zte(http, device_id))

    async def _lista_wifi_zte(self, http: httpx.AsyncClient, device_id: str, lan: Dict[str, Any], mac: str,
                              bloquear: bool, redes: List[int]) -> None:
        """La lista de bloqueo del WiFi. Bloquear: la MAC va a la lista de cada red indicada y esa
        lista queda en modo Ban, todo en una sola orden (la 2.4 y la 5 GHz juntas, nunca una sola).
        Desbloquear: se quita de todas; una lista que queda vacía vuelve a Disabled."""
        wlans = f"{LAN}.WLANConfiguration"
        prefijo = prefijo_zte(lan.get("WLANConfiguration"))
        if not prefijo:
            raise ValueError("La ONU no trae la lista de bloqueo de ZTE.")
        valores = []
        for indice, wlan in _instancias(lan.get("WLANConfiguration")):
            lista = f"{wlans}.{indice}.{prefijo}_AccessControl"
            modo = f"{wlans}.{indice}.{prefijo}_AccessControlMode"
            entradas = _instancias(wlan.get(f"{prefijo}_AccessControl"))
            macs = {n: _mac_o_none(_valor(e.get("MACAddress"))) for n, e in entradas}
            suyas = [n for n, m in macs.items() if m == mac]
            if bloquear:
                if indice not in redes:
                    continue
                if not suyas:
                    # Un lugar vacío que haya quedado de antes, o uno nuevo.
                    lugar = next((n for n, m in macs.items() if m is None), None)
                    if lugar is None:
                        lugar = await self.agregar_instancia(http, device_id, lista)
                    valores.append([f"{lista}.{lugar}.MACAddress", mac, "xsd:string"])
                valores.append([modo, "Ban", "xsd:string"])
            else:
                for n in suyas:
                    await self._obligada(http, device_id, {"name": "deleteObject", "objectName": f"{lista}.{n}"})
                if suyas and all(m in (None, mac) for m in macs.values()):
                    valores.append([modo, "Disabled", "xsd:string"])
        if valores:
            await self._obligada(http, device_id, {"name": "setParameterValues", "parameterValues": valores})

    async def _filtro_firewall_zte(self, http: httpx.AsyncClient, device_id: str, firewall: Dict[str, Any],
                                   mac: str, bloquear: bool) -> None:
        """El filtro MAC del firewall: el equipo sigue conectado a su WiFi, con su misma MAC, y
        sin internet. La ZTE solo acepta la MAC si la regla ya es de tipo Routing (en Bridge,
        como las crea, la rechaza) y rechaza una MAC que ya está en otra regla."""
        prefijo, filtro = filtro_mac_zte(firewall)
        if not filtro:
            raise ValueError("La ONU no trae el filtro MAC de ZTE.")
        servicio = f"{FIREWALL}.{prefijo}_MacFilterService"
        reglas = {n: _mac_o_none(_valor(r.get("SourceMACAddress"))) for n, r in _instancias(filtro.get("Filter"))}
        suyas = [n for n, m in reglas.items() if m == mac]
        if not bloquear:
            for n in suyas:
                await self._obligada(http, device_id, {"name": "deleteObject", "objectName": f"{servicio}.Filter.{n}"})
            return
        encendido = _valor(filtro.get("Enable")) in (True, "true", "1", 1)
        modo = str(_valor(filtro.get("Mode")) or "")
        if encendido and modo != "Black List":
            raise ValueError("El filtro MAC de la ONU está en lista blanca; ponlo en lista negra para bloquear equipos.")
        encender = [] if encendido and modo == "Black List" else [
            [f"{servicio}.Mode", "Black List", "xsd:string"], [f"{servicio}.Enable", True, "xsd:boolean"]]
        if not suyas:
            lugar = next((n for n, m in reglas.items() if m is None), None)
            nueva = lugar is None
            if nueva:
                lugar = await self.agregar_instancia(http, device_id, f"{servicio}.Filter")
            regla = f"{servicio}.Filter.{lugar}"
            try:
                await self._obligada(http, device_id, {"name": "setParameterValues", "parameterValues": [
                    [f"{regla}.Type", "Routing", "xsd:string"],
                    [f"{regla}.Protocol", "Any", "xsd:string"],
                    [f"{regla}.Name", "FdezPay-" + mac.replace(":", "")[-4:], "xsd:string"]]})
                # La MAC va después del tipo (en Bridge la rechaza) y con el filtro encendido en la misma orden.
                await self._obligada(http, device_id, {"name": "setParameterValues", "parameterValues": [
                    [f"{regla}.SourceMACAddress", mac, "xsd:string"]] + encender})
            except ValueError:
                if nueva:
                    await self._tarea(http, device_id, {"name": "deleteObject", "objectName": regla})
                raise
        elif encender:
            await self._obligada(http, device_id, {"name": "setParameterValues", "parameterValues": encender})

    async def _bloquear_zte(self, equipo: Dict[str, Any], mac: str, bloquear: bool, solo_principal: bool = False) -> tuple:
        """Bloquea o desbloquea en la ZTE. Devuelve (bloqueadas, redes WiFi usadas o [] si fue el firewall).

        En la red de la casa va el filtro MAC del firewall. Un equipo de otra red (la de fichas,
        en puente) va en la lista de bloqueo de sus WiFi. Desbloquear lo quita de los dos lados,
        y bloquear en el firewall lo quita de la lista del WiFi (bloqueos de antes)."""
        async with httpx.AsyncClient(base_url=self.nbi, timeout=ESPERA_ONU_MS / 1000 + 10) as http:
            device_id = await self._id_tr069(http, equipo)
            if not device_id:
                raise ValueError("La ONU del cliente todavía no se reporta por TR-069.")
            async with _candado(device_id):
                lan, firewall = await self._documento_zte(http, device_id)
                redes = redes_para_bloquear(lan, mac, solo_principal) if bloquear else []
                principales = indices_principales(redes_wifi(lan))
                otra_red = bool(redes and principales) and not set(redes) & principales
                if bloquear and otra_red:
                    # Para escribir en la lista del WiFi hay que tenerla al día (rechaza una MAC repetida).
                    lan, firewall = await self._documento_zte(http, device_id, wifi=True)
                    await self._lista_wifi_zte(http, device_id, lan, mac, True, redes)
                else:
                    await self._filtro_firewall_zte(http, device_id, firewall, mac, bloquear)
                    if mac in bloqueadas_zte(lan):
                        # Un bloqueo de antes en la lista del WiFi: se quita (hace que el celular estrene MAC).
                        lan, firewall = await self._documento_zte(http, device_id, wifi=True)
                        await self._lista_wifi_zte(http, device_id, lan, mac, False, [])
                    redes = []
                # GenieACS anota en su copia lo que la ONU aceptó: no hace falta volver a preguntarle.
                igd = (await self._leer_lan(http, device_id)).get("InternetGatewayDevice") or {}
                return bloqueadas_zte((igd.get("LANDevice") or {}).get("1") or {}, igd.get("Firewall") or {}), redes

    async def _bloqueadas(self, equipo: Dict[str, Any]) -> set:
        if equipo["marca"] == "zte":
            return await self._bloqueadas_zte(equipo)
        if equipo["marca"] != "huawei" or not equipo["ip"]:
            raise ValueError("El bloqueo de equipos solo está hecho para ONU Huawei y ZTE.")
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
            await self._marca_por_tr069(http, device_id, equipo)

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
                firewall = (documento.get("InternetGatewayDevice") or {}).get("Firewall") or {}
                if al_dia and equipo["marca"] == "zte" and filtro_mac_zte(firewall)[1] is None:
                    # Su filtro MAC (donde van los bloqueos) se lee una vez; luego lo mantiene al día el bloqueo.
                    await self._tarea(http, device_id, {"name": "refreshObject", "objectName": FIREWALL})
                    documento = await self._leer_lan(http, device_id)
                return documento, al_dia

            async def web():
                if equipo["marca"] == "zte":
                    return None, None  # Su lista de bloqueo viene en la misma lectura de TR-069.
                try:
                    return await self._bloqueadas(equipo), None
                except (ValueError, httpx.HTTPError) as error:
                    return None, str(error) if isinstance(error, ValueError) else "La web de la ONU no respondió."

            (documento, al_dia), (bloqueadas, error_web) = await asyncio.gather(tr069(), web())
        lan = ((documento or {}).get("InternetGatewayDevice") or {}).get("LANDevice", {}).get("1", {})
        if equipo["marca"] == "zte":
            bloqueadas = bloqueadas_zte(lan, ((documento or {}).get("InternetGatewayDevice") or {}).get("Firewall")) \
                if device_id else None
            error_web = None if device_id else "La ONU no se reporta por TR-069."
        redes = redes_wifi(lan)
        lista = equipos(lan, redes, bloqueadas, await self.apodos(cliente_id))
        if solo_principal:
            redes, lista = solo_de_la_red_principal(redes, lista)
        return {
            "ip": equipo["ip"],
            "onu": equipo["identificador"],
            # El del inventario; si no lo tiene, el que reporta la ONU por TR-069.
            "modelo": equipo["modelo"] or modelo_tr069(documento, equipo["marca"]),
            "marca": equipo["marca"],
            "tr069": bool(device_id),
            "al_dia": al_dia,
            "ultimo_reporte": (documento or {}).get("_lastInform"),
            "equipos": [{k: v for k, v in e.items() if k != "indice_red"} for e in lista],
            "redes": [{k: v for k, v in r.items() if k != "macs"}
                      | {"conectados": sum(1 for e in lista if e["conectado"] and e["indice_red"] == r["indice"])} for r in redes],
            "bloqueo_disponible": bloqueadas is not None,
            "error_bloqueo": error_web,
        }

    async def apodos(self, cliente_id: int) -> Dict[str, str]:
        filas = (await self.db.execute(
            select(EquipoNombreModel).where(EquipoNombreModel.cliente_id == cliente_id)
        )).scalars().all()
        return {f.mac: f.nombre for f in filas}

    async def nombrar(self, cliente_id: int, mac: str, nombre: str) -> Dict[str, Any]:
        """Le pone nombre a un equipo; vacío se lo quita. Solo se guarda en el sistema, no en la ONU."""
        mac = normalizar_mac(mac)
        nombre = " ".join((nombre or "").split())
        if len(nombre) > MAX_APODO:
            raise ValueError(f"El nombre puede tener hasta {MAX_APODO} letras.")
        fila = await self.db.get(EquipoNombreModel, (cliente_id, mac))
        if not nombre:
            if fila:
                await self.db.delete(fila)
        elif fila:
            fila.nombre = nombre
        else:
            self.db.add(EquipoNombreModel(cliente_id=cliente_id, mac=mac, nombre=nombre))
        await self.db.commit()
        return {"mac": mac, "apodo": nombre or None}

    async def bloqueados(self, cliente_id: int, servicio_id: Optional[int] = None) -> Dict[str, Any]:
        """Solo los filtros de la web de la ONU (unos segundos): para confirmar un bloqueo cuya
        respuesta no llegó porque la ONU reinició el WiFi y cortó al celular que lo pidió."""
        return {"bloqueados": sorted(await self._bloqueadas(await self._equipo_con_marca(cliente_id, servicio_id)))}

    async def bloquear(self, cliente_id: int, mac: str, bloquear: bool, usuario=None, servicio_id: Optional[int] = None,
                       solo_principal: bool = False) -> Dict[str, Any]:
        mac = normalizar_mac(mac)
        equipo = await self._equipo_con_marca(cliente_id, servicio_id)
        if equipo["marca"] == "zte":
            bloqueadas, redes = await self._bloquear_zte(equipo, mac, bloquear, solo_principal)
            await self._bitacora(usuario, "Bloquear equipo" if bloquear else "Desbloquear equipo", cliente_id,
                                 f"Cliente {cliente_id} · ONU {equipo['identificador'] or equipo['ip']} · {mac}"
                                 + (f" · WiFi {', '.join(map(str, redes))}" if redes else
                                    " · filtro MAC" if bloquear else "") + " · TR-069")
            return {"bloqueados": sorted(bloqueadas)}
        if equipo["marca"] != "huawei" or not equipo["ip"]:
            raise ValueError("El bloqueo de equipos solo está hecho para ONU Huawei y ZTE.")
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

    async def cambiar_clave_wifi(self, cliente_id: int, ssid: Optional[str], clave: str, usuario=None,
                                 servicio_id: Optional[int] = None, solo_principal: bool = False) -> Dict[str, Any]:
        """Cambia la contraseña en todas las bandas que usan ese nombre de red (sin nombre: la principal)."""
        if not RE_CLAVE_WIFI.match(clave or ""):
            raise ValueError("La contraseña debe tener de 8 a 63 caracteres, sin acentos ni ñ.")
        equipo = await self._equipo(cliente_id, servicio_id)
        async with httpx.AsyncClient(base_url=self.nbi, timeout=ESPERA_ONU_MS / 1000 + 10) as http:
            device_id = await self._id_tr069(http, equipo)
            if not device_id:
                raise ValueError("La ONU del cliente todavía no se reporta por TR-069.")
            lan = ((await self._leer_lan(http, device_id)).get("InternetGatewayDevice") or {}).get("LANDevice", {}).get("1", {})
            redes = redes_wifi(lan)
            if ssid is None:
                ssid = next((r["ssid"] for r in redes if r["indice"] in indices_principales(redes)), None)
            indices = [r["indice"] for r in redes if r["ssid"] == ssid]
            if not indices:
                raise ValueError("Esa red WiFi no está en la ONU.")
            if solo_principal and not set(indices) <= indices_principales(redes):
                raise ValueError("Solo puedes cambiar la contraseña de tu red principal.")
            aplicado = await self.poner_con_clave_wifi(http, device_id, indices, clave)
        if set(indices) <= indices_principales(redes):
            await self._recordar_wifi(cliente_id, equipo["identificador"] or "", ssid, clave)
        await self._bitacora(usuario, "Cambiar contraseña WiFi", cliente_id,
                             f"Cliente {cliente_id} · ONU {equipo['identificador'] or equipo['ip']} · red {ssid}")
        return {"aplicado": aplicado, "bandas": len(indices), "ssid": ssid}

    async def poner_con_clave_wifi(self, http: httpx.AsyncClient, device_id: str, indices: List[int], clave: str,
                                   antes: Optional[list] = None, despues: Optional[list] = None) -> bool:
        """La contraseña del WiFi en esas redes (con otros valores en la misma orden), en el campo que acepte la ONU."""
        for n, campo in enumerate(CAMPOS_CLAVE_WIFI):
            valores = (list(antes or []) + [[f"{LAN}.WLANConfiguration.{i}.{campo}", clave, "xsd:string"] for i in indices]
                       + list(despues or []))
            try:
                return await self._tarea(http, device_id, {"name": "setParameterValues", "parameterValues": valores})
            except ValueError:
                if n == len(CAMPOS_CLAVE_WIFI) - 1:
                    raise
        return False

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

    async def _recordar_wifi(self, cliente_id: int, identificador: str, ssid: str, clave: str) -> None:
        """La ONU no deja leer la contraseña: se guarda para ponérsela igual a la ONU de reemplazo."""
        registro = (await self.db.execute(
            select(AprovisionamientoOnuModel).where(AprovisionamientoOnuModel.cliente_id == cliente_id)
            .order_by(AprovisionamientoOnuModel.id.desc()).limit(1)
        )).scalars().first()
        if registro and registro.estado in ("aplicado", "guardado", "manual"):
            registro.ssid, registro.clave_wifi = ssid[:32], clave
        else:
            self.db.add(AprovisionamientoOnuModel(cliente_id=cliente_id, identificador=identificador[:100],
                                                  ssid=ssid[:32], clave_wifi=clave, estado="guardado"))
        await self.db.commit()

    async def _bitacora(self, usuario, accion: str, cliente_id: int, detalle: str) -> None:
        self.db.add(LogActividadModel(
            usuario_id=getattr(usuario, "id", None), usuario_nombre=getattr(usuario, "usuario", None),
            accion=f"Red del cliente: {accion}", metodo="POST", ruta=f"/api/clientes/{cliente_id}/red",
            estado_http=200, detalle=detalle[:1000],
        ))
        await self.db.commit()
