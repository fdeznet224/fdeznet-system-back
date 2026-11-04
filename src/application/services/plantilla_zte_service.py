"""La ZTE que entra al ACS queda como la Huawei que sale del almacén.

La Huawei trae su plantilla (ConfigF-TR069.xml) desde el almacén. La ZTE no:
llega con su WAN de internet (PPPoE + ACS) y aquí se le pone, por TR-069,
lo mismo que tiene la Huawei:

  - WAN de fichas: puente (IP_Bridged) en la VLAN de fichas, servicio INTERNET.
  - Red de fichas abierta ("Zona FdezNet") en 2.4 y 5 GHz, si no la trae.
  - Enlace de puertos 1: el puerto LAN de fichas y las redes de fichas → WAN de fichas.
  - Enlace de puertos 2: los demás LAN y las demás redes encendidas → WAN de internet.

Se revisa cada vez que la ONU arranca (BOOT o BOOTSTRAP), porque al restablecerla
pierde la WAN de fichas y los enlaces aunque conserve el WiFi. Lo que ya está bien
no se toca.

Cada firmware tiene sus mañas (probadas el 3 y 4 nov 2026 con tres F670L):
  - El prefijo de sus parámetros es X_ZTE-COM_ o X_TELMEX_COM_, y cambia con el firmware.
  - No deja crear otro WANConnectionDevice (9002): la WAN de fichas va como
    otra WANIPConnection dentro del de internet, con la VLAN en la conexión.
  - Mientras hay una WAN PPPoE_Bridged de fichas no acepta la nueva: se quita.
  - Una red abierta se enciende con BeaconType "None" en unos y "Basic" en otros,
    algunas redes no encienden (WLAN 2 en uno) y otras dicen que sí pero no cambian
    (5 GHz en otro): por eso se vuelve a leer lo que quedó.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.services.red_cliente_service import (
    LAN, RedClienteService, _instancias, _valor, prefijo_zte,
)
from src.infrastructure.models import ConfiguracionModel, LogCronjobModel

logger = logging.getLogger(__name__)

IGD = "InternetGatewayDevice"
CONEXIONES = f"{IGD}.WANDevice.1.WANConnectionDevice"
LAN_ETH = f"{LAN}.LANEthernetInterfaceConfig"
WLANS = f"{LAN}.WLANConfiguration"
PUERTOS_LAN = (1, 2, 3, 4)
# Solo las que se reportaron hace poco: a las demás no se les puede avisar.
RECIENTE = timedelta(minutes=10)
# Si algo falló, no se insiste cada vuelta: se vuelve a intentar pasado este tiempo.
REINTENTO = timedelta(minutes=30)
_ultimo_intento: Dict[str, datetime] = {}

PREDETERMINADO = {"activa": "1", "nombre": "Zona FdezNet", "vlan": "4", "lan": "4"}


def _clave(campo: str) -> str:
    return f"tr069_fichas_{campo}"


async def config_fichas(db: AsyncSession) -> Dict[str, Any]:
    """La red de fichas de este ISP (Configuración; sin nada guardado, la de FdezNet)."""
    filas = (await db.execute(
        select(ConfiguracionModel).where(ConfiguracionModel.clave.in_([_clave(c) for c in PREDETERMINADO]))
    )).scalars().all()
    guardado = {f.clave: (f.valor or "").strip() for f in filas}
    valores = {c: guardado.get(_clave(c)) or v for c, v in PREDETERMINADO.items()}
    return {
        "activa": valores["activa"] not in ("0", "no", "false"),
        "nombre": valores["nombre"],
        "vlan": int(valores["vlan"]),
        "lan": sorted({int(p) for p in valores["lan"].replace(" ", "").split(",") if p.isdigit() and int(p) in PUERTOS_LAN}),
    }


# ------------------------------------------------------------------ lectura
def _ruta(base: str, *partes: Any) -> str:
    return ".".join([base] + [str(p) for p in partes])


def _limpia(ruta: str) -> str:
    return (ruta or "").strip().rstrip(".")


def _encendida(nodo: Dict[str, Any], campo: str = "Enable") -> bool:
    return str(_valor(nodo.get(campo))).lower() in ("true", "1")


def wan_internet(igd: Dict[str, Any], prefijo: str) -> Optional[Tuple[int, str]]:
    """(WANConnectionDevice, ruta) de la WAN PPPoE ruteada que lleva el internet."""
    for i, dispositivo in _instancias(((igd.get("WANDevice") or {}).get("1") or {}).get("WANConnectionDevice")):
        for j, ppp in _instancias(dispositivo.get("WANPPPConnection")):
            servicio = str(_valor(ppp.get(f"{prefijo}_ServiceList")) or "")
            if "INTERNET" in servicio and str(_valor(ppp.get("ConnectionType"))) == "IP_Routed":
                return i, _ruta(CONEXIONES, i, "WANPPPConnection", j)
    return None


def wan_fichas(igd: Dict[str, Any], prefijo: str, vlan: int) -> Optional[str]:
    """La WAN puente de fichas: IP_Bridged en su VLAN."""
    for i, dispositivo in _instancias(((igd.get("WANDevice") or {}).get("1") or {}).get("WANConnectionDevice")):
        for j, ip in _instancias(dispositivo.get("WANIPConnection")):
            if (str(_valor(ip.get("ConnectionType"))) == "IP_Bridged"
                    and str(_valor(ip.get(f"{prefijo}_VLANID"))) == str(vlan)):
                return _ruta(CONEXIONES, i, "WANIPConnection", j)
    return None


def puentes_pppoe_de_fichas(igd: Dict[str, Any], nombre: str) -> List[str]:
    """Una WAN PPPoE_Bridged con el nombre de fichas (así la dejan desde la web): estorba a la nueva."""
    rutas = []
    for i, dispositivo in _instancias(((igd.get("WANDevice") or {}).get("1") or {}).get("WANConnectionDevice")):
        for j, ppp in _instancias(dispositivo.get("WANPPPConnection")):
            if str(_valor(ppp.get("ConnectionType"))) == "PPPoE_Bridged" and str(_valor(ppp.get("Name"))) == nombre:
                rutas.append(_ruta(CONEXIONES, i, "WANPPPConnection", j))
    return rutas


def banda_de(indice: int, wlan: Dict[str, Any], prefijo: str) -> str:
    banda = str(_valor(wlan.get(f"{prefijo}_OperatingFrequencyBand")) or "")
    return "5GHz" if banda.startswith("5") or (not banda and indice > 4) else "2.4GHz"


def redes(lan: Dict[str, Any], prefijo: str, nombre_fichas: str) -> Tuple[Dict[str, List[int]], List[int]]:
    """({banda: redes de fichas encendidas}, redes encendidas de la casa)."""
    fichas: Dict[str, List[int]] = {"2.4GHz": [], "5GHz": []}
    casa = []
    for indice, wlan in _instancias(lan.get("WLANConfiguration")):
        if not _encendida(wlan):
            continue
        if str(_valor(wlan.get("SSID"))) == nombre_fichas:
            fichas[banda_de(indice, wlan, prefijo)].append(indice)
        else:
            casa.append(indice)
    return fichas, casa


def enlaces(igd: Dict[str, Any], prefijo: str) -> Dict[int, Tuple[str, set]]:
    """{número: (WAN, {LAN y WiFi})} del enlace de puertos."""
    salida = {}
    for n, enlace in _instancias(igd.get(f"{prefijo}_PortBinding")):
        lan = {_limpia(p) for p in str(_valor(enlace.get("LANInterface")) or "").split(",") if p.strip()}
        salida[n] = (_limpia(str(_valor(enlace.get("WANInterface")) or "")), lan)
    return salida


def enlaces_deseados(igd: Dict[str, Any], prefijo: str, config: Dict[str, Any]) -> Optional[Dict[str, Tuple[str, set]]]:
    """{"fichas": (WAN, puertos), "internet": (WAN, puertos)}; None si falta alguna WAN."""
    internet = wan_internet(igd, prefijo)
    fichas = wan_fichas(igd, prefijo, config["vlan"])
    if not internet or not fichas:
        return None
    lan = (igd.get("LANDevice") or {}).get("1") or {}
    de_fichas, de_casa = redes(lan, prefijo, config["nombre"])
    puertos_fichas = {_ruta(LAN_ETH, p) for p in config["lan"]}
    return {
        "fichas": (fichas, puertos_fichas | {_ruta(WLANS, i) for b in de_fichas.values() for i in b}),
        "internet": (internet[1], {_ruta(LAN_ETH, p) for p in PUERTOS_LAN if p not in config["lan"]}
                     | {_ruta(WLANS, i) for i in de_casa}),
    }


def lo_que_falta(igd: Dict[str, Any], config: Dict[str, Any]) -> List[str]:
    """Lo que le falta a la ONU según lo último leído (vacío: está completa)."""
    prefijo = prefijo_zte(igd)
    if not prefijo:
        return ["no es ZTE"]
    faltan = []
    if not wan_internet(igd, prefijo):
        faltan.append("WAN de internet")
    if not wan_fichas(igd, prefijo, config["vlan"]):
        faltan.append("WAN de fichas")
    # La red de fichas no cuenta: hay firmware que no deja encender alguna banda.
    deseados = enlaces_deseados(igd, prefijo, config)
    if deseados:
        actuales = enlaces(igd, prefijo).values()
        faltan += [f"enlace de {nombre}" for nombre, (wan, puertos) in deseados.items()
                   if (wan, puertos) not in [(w, p) for w, p in actuales]]
    return faltan


def leida_despues_de_arrancar(documento: Dict[str, Any]) -> bool:
    """¿Lo guardado de su WAN es de después de su último arranque? Si no, puede ya no estar."""
    wan = ((((documento.get(IGD) or {}).get("WANDevice") or {}).get("1") or {}).get("WANConnectionDevice") or {})
    leida = str(wan.get("_timestamp") or "")
    arranque = max(str(documento.get("_lastBoot") or ""), str(documento.get("_lastBootstrap") or ""))
    return bool(leida) and leida > arranque


# ------------------------------------------------------------------ servicio
class PlantillaZteService:
    def __init__(self, db: AsyncSession, nbi: Optional[str] = None, transporte=None):
        self.db = db
        self.red = RedClienteService(db, nbi)
        self.transporte = transporte

    async def revisar(self) -> Dict[str, int]:
        """Una vuelta: las ZTE que se reportaron hace poco y arrancaron desde su última revisión."""
        config = await config_fichas(self.db)
        conteo = {"revisadas": 0, "configuradas": 0, "errores": 0}
        if not config["activa"]:
            return conteo
        desde = (_ahora() - RECIENTE).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        async with httpx.AsyncClient(base_url=self.red.nbi, timeout=60, transport=self.transporte) as http:
            respuesta = await http.get("/devices", params={
                "query": json.dumps({"$or": [{"_deviceId._Manufacturer": {"$regex": "ZTE", "$options": "i"}},
                                             {"_deviceId._SerialNumber": {"$regex": "^ZTEG"}}],
                                     "_lastInform": {"$gt": desde}}),
                "projection": f"_id,_lastInform,_lastBoot,_lastBootstrap,{IGD}",
            })
            respuesta.raise_for_status()
            for documento in respuesta.json():
                device_id = documento["_id"]
                igd = documento.get(IGD) or {}
                if leida_despues_de_arrancar(documento) and not lo_que_falta(igd, config):
                    continue
                intento = _ultimo_intento.get(device_id)
                if intento and _ahora() - intento < REINTENTO:
                    continue
                _ultimo_intento[device_id] = _ahora()
                conteo["revisadas"] += 1
                try:
                    resultado = await self.aplicar(http, device_id, config)
                except (ValueError, httpx.HTTPError) as error:
                    resultado = {"hecho": [], "avisos": [], "faltan": [str(error) or "sin respuesta"]}
                if resultado["faltan"]:
                    conteo["errores"] += 1
                else:
                    _ultimo_intento.pop(device_id, None)
                    if resultado["hecho"]:
                        conteo["configuradas"] += 1
                if resultado["hecho"] or resultado["faltan"] or resultado.get("avisos"):
                    await self._registrar(device_id, resultado)
        return conteo

    async def aplicar(self, http: httpx.AsyncClient, device_id: str, config: Dict[str, Any]) -> Dict[str, List[str]]:
        """Le pone a la ONU lo que le falte y vuelve a leer: {"hecho", "avisos", "faltan"}."""
        hecho: List[str] = []
        avisos: List[str] = []
        igd = await self._releer(http, device_id)
        prefijo = prefijo_zte(igd)
        if not prefijo:
            return {"hecho": [], "avisos": [], "faltan": ["no trae parámetros de ZTE"]}
        internet = wan_internet(igd, prefijo)
        if not internet:
            return {"hecho": [], "avisos": [], "faltan": ["WAN de internet (PPPoE)"]}

        if not wan_fichas(igd, prefijo, config["vlan"]):
            for estorbo in puentes_pppoe_de_fichas(igd, config["nombre"]):
                await self.red._obligada(http, device_id, {"name": "deleteObject", "objectName": estorbo})
                hecho.append("quitó la WAN PPPoE_Bridged de fichas")
            await self._crear_wan_fichas(http, device_id, internet[0], prefijo, config)
            hecho.append(f"WAN de fichas (puente, VLAN {config['vlan']})")
            igd = await self._releer(http, device_id)

        lan = (igd.get("LANDevice") or {}).get("1") or {}
        de_fichas, _ = redes(lan, prefijo, config["nombre"])
        for banda, indices in de_fichas.items():
            if indices:
                continue
            if await self._crear_red_fichas(http, device_id, lan, prefijo, banda, config["nombre"]):
                hecho.append(f"red de fichas {banda}")
            else:
                avisos.append(f"su firmware no dejó encender la red de fichas {banda}")
        if any(h.startswith("red de fichas") for h in hecho):
            igd = await self._releer(http, device_id)

        deseados = enlaces_deseados(igd, prefijo, config) or {}
        actuales = enlaces(igd, prefijo)
        for nombre, (wan, puertos) in deseados.items():
            existente = next((n for n, (w, _) in actuales.items() if w == wan), None)
            if existente is not None and actuales[existente][1] == puertos:
                continue
            base = f"{IGD}.{prefijo}_PortBinding"
            if existente is None:
                existente = await self.red.agregar_instancia(http, device_id, base)
            await self.red._obligada(http, device_id, {"name": "setParameterValues", "parameterValues": [
                [f"{base}.{existente}.WANInterface", wan, "xsd:string"],
                [f"{base}.{existente}.LANInterface", ",".join(sorted(puertos)), "xsd:string"],
            ]})
            hecho.append(f"enlace de {nombre}")

        igd = await self._releer(http, device_id)
        faltan = lo_que_falta(igd, config)
        fichas = wan_fichas(igd, prefijo, config["vlan"])
        if fichas and not faltan:
            estado = _valor(_nodo(igd, fichas).get("ConnectionStatus"))
            if estado and estado != "Connected":
                faltan.append(f"WAN de fichas {estado}")
        return {"hecho": hecho, "avisos": avisos, "faltan": faltan}

    async def _releer(self, http: httpx.AsyncClient, device_id: str) -> Dict[str, Any]:
        for objeto in (f"{IGD}.WANDevice.1.WANConnectionDevice", WLANS):
            await self.red._obligada(http, device_id, {"name": "refreshObject", "objectName": objeto})
        documento = await self._documento(http, device_id)
        prefijo = prefijo_zte(documento.get(IGD) or {})
        if prefijo:
            await self.red._obligada(http, device_id, {"name": "refreshObject", "objectName": f"{IGD}.{prefijo}_PortBinding"})
            documento = await self._documento(http, device_id)
        return documento.get(IGD) or {}

    async def _documento(self, http: httpx.AsyncClient, device_id: str) -> Dict[str, Any]:
        respuesta = await http.get("/devices", params={"query": json.dumps({"_id": device_id}), "projection": IGD})
        respuesta.raise_for_status()
        return (respuesta.json() or [{}])[0]

    async def _crear_wan_fichas(self, http, device_id: str, dispositivo: int, prefijo: str, config) -> None:
        base = _ruta(CONEXIONES, dispositivo, "WANIPConnection")
        ruta = _ruta(base, await self.red.agregar_instancia(http, device_id, base))
        # Por partes: si una falla se sabe cuál.
        for valores in (
            [[f"{ruta}.ConnectionType", "IP_Bridged", "xsd:string"], [f"{ruta}.Name", config["nombre"], "xsd:string"],
             [f"{ruta}.NATEnabled", False, "xsd:boolean"]],
            [[f"{ruta}.{prefijo}_VLANEnable", True, "xsd:boolean"], [f"{ruta}.{prefijo}_VLANID", config["vlan"], "xsd:unsignedInt"],
             [f"{ruta}.{prefijo}_ServiceList", "INTERNET", "xsd:string"]],
            [[f"{ruta}.Enable", True, "xsd:boolean"]],
        ):
            await self.red._obligada(http, device_id, {"name": "setParameterValues", "parameterValues": valores})

    async def _crear_red_fichas(self, http, device_id: str, lan, prefijo: str, banda: str, nombre: str) -> bool:
        """En la primera red apagada de esa banda que acepte encenderse abierta; True si quedó."""
        for indice, wlan in _instancias(lan.get("WLANConfiguration")):
            if indice == 1 or _encendida(wlan) or banda_de(indice, wlan, prefijo) != banda:
                continue
            ruta = _ruta(WLANS, indice)
            try:
                await self.red._obligada(http, device_id, {"name": "setParameterValues", "parameterValues": [
                    [f"{ruta}.SSID", nombre, "xsd:string"], [f"{ruta}.BasicAuthenticationMode", "None", "xsd:string"],
                    [f"{ruta}.BasicEncryptionModes", "None", "xsd:string"],
                    [f"{ruta}.SSIDAdvertisementEnabled", True, "xsd:boolean"]]})
            except ValueError:
                continue
            for seguridad in ("None", "Basic"):
                try:
                    await self.red._obligada(http, device_id, {"name": "setParameterValues", "parameterValues": [
                        [f"{ruta}.BeaconType", seguridad, "xsd:string"]]})
                    await self.red._obligada(http, device_id, {"name": "setParameterValues", "parameterValues": [
                        [f"{ruta}.Enable", True, "xsd:boolean"]]})
                except ValueError:
                    continue
                # Algunas dicen que sí y no cambian: se pregunta lo que quedó.
                await self.red._obligada(http, device_id, {"name": "getParameterValues",
                                                           "parameterNames": [f"{ruta}.Enable", f"{ruta}.SSID"]})
                quedo = _nodo((await self._documento(http, device_id)).get(IGD) or {}, ruta)
                if _encendida(quedo) and str(_valor(quedo.get("SSID"))) == nombre:
                    return True
                break
        return False

    async def _registrar(self, device_id: str, resultado: Dict[str, List[str]]) -> None:
        partes = []
        if resultado["hecho"]:
            partes.append("puso " + ", ".join(resultado["hecho"]))
        if resultado.get("avisos"):
            partes.append(", ".join(resultado["avisos"]))
        if resultado["faltan"]:
            partes.append("falta " + ", ".join(resultado["faltan"]))
        mensaje = f"Plantilla ZTE {device_id}: " + "; ".join(partes)
        logger.info(mensaje)
        self.db.add(LogCronjobModel(nivel="WARN" if resultado["faltan"] else "INFO", origen="TR-069", mensaje=mensaje[:500]))
        await self.db.commit()


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def _nodo(igd: Dict[str, Any], ruta: str) -> Dict[str, Any]:
    nodo: Any = {IGD: igd}
    for parte in ruta.split("."):
        nodo = (nodo or {}).get(parte) or {}
    return nodo
