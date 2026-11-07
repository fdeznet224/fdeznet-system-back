"""Lo mismo que el acceso remoto a la web del módem, pero por TR-069 (GenieACS).

El admin ve y cambia, desde Infraestructura → TR-069 (por equipo):
  - WAN: cada conexión (PPPoE o IP), ruta o puente, VLAN, servicio, usuario PPPoE,
    encender/apagar, agregar y borrar.
  - Enlace de puertos: qué LAN y qué redes WiFi salen por cada WAN.
  - WiFi: nombre, encender/apagar, ocultar, canal y ancho de cada red.
  - LAN: IP del módem, DHCP (rango, tiempo, DNS), puertos abiertos (NAT) y DMZ.

Rutas sacadas de ONU reales el 7 nov 2026 (HG8145V5 V5R019 del dueño y F670L):
  - Huawei: cada WAN va en su propio WANConnectionDevice, con X_HW_VLAN y X_HW_SERVICELIST.
    El enlace de puertos está en dos lugares: X_HW_LANBIND (Lan1-4, SSID1-4) en la conexión
    y Layer3Forwarding.X_HW_policy_route (PhyPortName "LAN1,SSID5", WanName "wan1.2.ip1"),
    que es el único que trae las redes de 5 GHz. Se escriben los dos.
    El ancho del WiFi es X_HW_HT20 (0=20, 1=40, 2=20/40, 3=80 MHz) y la DMZ va por WAN (X_HW_DMZ).
  - ZTE: todas las WAN dentro de WANConnectionDevice.1 (crear otro da 9002), con
    {prefijo}_VLANEnable/VLANID/ServiceList, y el enlace en {prefijo}_PortBinding.N
    (LANInterface con rutas completas separadas por coma, WANInterface). El prefijo
    (X_ZTE-COM o X_TELMEX_COM) se averigua en cada lectura.

La WAN que lleva el TR-069 no se apaga, ni se pasa a puente, ni se le cambia la VLAN, ni se
borra: el módem se quedaría sin control remoto y habría que ir a la casa.
"""

import ipaddress
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.models import LogActividadModel
from src.application.services.red_cliente_service import (
    ESPERA_ONU_MS, LAN, RedClienteService, _candado, _instancias, _valor, prefijo_zte,
)

logger = logging.getLogger(__name__)

IGD = "InternetGatewayDevice"
CONEXIONES = f"{IGD}.WANDevice.1.WANConnectionDevice"
HOST = f"{LAN}.LANHostConfigManagement"
WLANS = f"{LAN}.WLANConfiguration"
LAN_ETH = f"{LAN}.LANEthernetInterfaceConfig"
RUTAS = f"{IGD}.Layer3Forwarding.X_HW_policy_route"
# X_HW_HT20 de la Huawei: 2.4 GHz 0/1/2; 5 GHz 0/1/3 y 4 (160 MHz) en las WiFi 6 (X6-10).
ANCHOS_HUAWEI = {"0": "20 MHz", "1": "40 MHz", "2": "20/40 MHz", "3": "80 MHz", "4": "160 MHz"}


def anchos_huawei(wlan: Dict[str, Any], banda: str) -> Dict[str, str]:
    claves = ["0", "1", "2"] if banda == "2.4 GHz" else ["0", "1", "3"]
    if banda == "5 GHz" and "ax" in (_texto(wlan, "X_HW_SupportedStandards") + _texto(wlan, "Standard")).lower():
        claves.append("4")
    actual = _texto(wlan, "X_HW_HT20")
    if actual in ANCHOS_HUAWEI and actual not in claves:
        claves.append(actual)
    return {c: ANCHOS_HUAWEI[c] for c in sorted(claves)}
RE_RUTA_WAN = re.compile(rf"^{re.escape(CONEXIONES)}\.(\d+)\.(WANPPPConnection|WANIPConnection)\.(\d+)$")
RE_PUERTO = re.compile(r"^(LAN|SSID)(\d+)$")
PROTOCOLOS = ("TCP", "UDP")


def _texto(nodo: Dict[str, Any], campo: str) -> str:
    valor = _valor(nodo.get(campo))
    return "" if valor is None else str(valor)


def _si(nodo: Dict[str, Any], campo: str) -> bool:
    return _texto(nodo, campo).lower() in ("true", "1")


def _nodo(igd: Dict[str, Any], ruta: str) -> Dict[str, Any]:
    nodo: Any = {IGD: igd}
    for parte in ruta.split("."):
        nodo = (nodo or {}).get(parte) if isinstance(nodo, dict) else None
    return nodo if isinstance(nodo, dict) else {}


def _ip(texto: str, campo: str) -> str:
    try:
        return str(ipaddress.IPv4Address((texto or "").strip()))
    except ValueError as error:
        raise ValueError(f"{campo}: «{texto}» no es una IP válida.") from error


def _vlan(valor: Any) -> int:
    try:
        vlan = int(valor)
    except (TypeError, ValueError) as error:
        raise ValueError("La VLAN tiene que ser un número.") from error
    if not 1 <= vlan <= 4094:
        raise ValueError("La VLAN va de 1 a 4094.")
    return vlan


def _puerto_tcp(valor: Any, campo: str) -> int:
    try:
        puerto = int(valor)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{campo}: tiene que ser un número.") from error
    if not 1 <= puerto <= 65535:
        raise ValueError(f"{campo}: va de 1 a 65535.")
    return puerto


# ------------------------------------------------------------------ lectura
def marca_de(igd: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    prefijo = prefijo_zte(igd)
    return ("zte", prefijo) if prefijo else ("huawei", None)


def nombre_wan_huawei(ruta: str) -> str:
    """"…WANConnectionDevice.2.WANIPConnection.1" → "wan1.2.ip1", como lo nombra su policy_route."""
    i, tipo, j = RE_RUTA_WAN.match(ruta).groups()
    return f"wan1.{i}.{'ppp' if tipo == 'WANPPPConnection' else 'ip'}{j}"


def puerto_de_ruta(ruta: str) -> Optional[str]:
    """Ruta completa de la ZTE → "LAN1" / "SSID5"."""
    m = re.search(r"(LANEthernetInterfaceConfig|WLANConfiguration)\.(\d+)$", (ruta or "").strip().rstrip("."))
    if not m:
        return None
    return f"{'LAN' if m.group(1).startswith('LANE') else 'SSID'}{m.group(2)}"


def ruta_de_puerto(puerto: str) -> str:
    tipo, n = RE_PUERTO.match(puerto).groups()
    return f"{LAN_ETH if tipo == 'LAN' else WLANS}.{n}"


def _orden_puerto(puerto: str) -> Tuple[int, int]:
    tipo, n = RE_PUERTO.match(puerto).groups()
    return (0 if tipo == "LAN" else 1, int(n))


def enlaces_actuales(igd: Dict[str, Any], marca: str, prefijo: Optional[str]) -> Dict[str, List[str]]:
    """{ruta de la WAN: ["LAN1", "SSID1", …]}."""
    salida: Dict[str, set] = {}
    if marca == "zte":
        for _, enlace in _instancias(igd.get(f"{prefijo}_PortBinding")):
            wan = _texto(enlace, "WANInterface").strip().rstrip(".")
            puertos = {puerto_de_ruta(p) for p in _texto(enlace, "LANInterface").split(",")}
            if wan:
                salida.setdefault(wan, set()).update(p for p in puertos if p)
        return {w: sorted(p, key=_orden_puerto) for w, p in salida.items()}
    nombres = {}
    for ruta, conexion in conexiones_wan(igd):
        nombres[nombre_wan_huawei(ruta)] = ruta
        enlace = conexion.get("X_HW_LANBIND") or {}
        for campo, valor in enlace.items():
            m = re.fullmatch(r"(Lan|SSID)(\d+)Enable", campo)
            if m and str(_valor(valor)) in ("1", "true", "True"):
                salida.setdefault(ruta, set()).add(f"{m.group(1).upper()}{m.group(2)}")
    for _, regla in _instancias(_nodo(igd, RUTAS)):
        ruta = nombres.get(_texto(regla, "WanName"))
        if ruta and _texto(regla, "PolicyRouteType") == "SourcePhyPort":
            salida.setdefault(ruta, set()).update(
                p.strip().upper() for p in _texto(regla, "PhyPortName").split(",") if RE_PUERTO.match(p.strip().upper()))
    return {w: sorted(p, key=_orden_puerto) for w, p in salida.items()}


def conexiones_wan(igd: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    salida = []
    for i, dispositivo in _instancias(_nodo(igd, CONEXIONES)):
        for tipo in ("WANPPPConnection", "WANIPConnection"):
            for j, conexion in _instancias(dispositivo.get(tipo)):
                salida.append((f"{CONEXIONES}.{i}.{tipo}.{j}", conexion))
    return salida


def _servicio(conexion: Dict[str, Any], marca: str, prefijo: Optional[str]) -> str:
    return _texto(conexion, "X_HW_SERVICELIST" if marca == "huawei" else f"{prefijo}_ServiceList")


def _vlan_de(conexion: Dict[str, Any], marca: str, prefijo: Optional[str]) -> Optional[int]:
    texto = _texto(conexion, "X_HW_VLAN" if marca == "huawei" else f"{prefijo}_VLANID")
    if marca == "zte" and conexion.get(f"{prefijo}_VLANEnable") is not None and not _si(conexion, f"{prefijo}_VLANEnable"):
        return None
    return int(texto) if texto.isdigit() and 0 < int(texto) < 4095 else None


def ip_de_gestion(documento: Dict[str, Any]) -> Optional[str]:
    url = _texto(_nodo(documento.get(IGD) or {}, f"{IGD}.ManagementServer"), "ConnectionRequestURL")
    m = re.match(r"^https?://([^:/]+)", url)
    return m.group(1) if m else None


def lleva_tr069(conexion: Dict[str, Any], servicio: str, ip_gestion: Optional[str]) -> bool:
    return "TR069" in servicio.upper() or bool(ip_gestion and _texto(conexion, "ExternalIPAddress") == ip_gestion)


def wans(igd: Dict[str, Any], marca: str, prefijo: Optional[str], ip_gestion: Optional[str]) -> List[Dict[str, Any]]:
    enlaces = enlaces_actuales(igd, marca, prefijo)
    salida = []
    for ruta, conexion in conexiones_wan(igd):
        ppp = ".WANPPPConnection." in ruta
        tipo_conexion = _texto(conexion, "ConnectionType")
        servicio = _servicio(conexion, marca, prefijo)
        dmz = conexion.get("X_HW_DMZ") if marca == "huawei" else None
        salida.append({
            "ruta": ruta,
            "nombre": _texto(conexion, "Name") or ruta.rsplit(".", 2)[-2],
            "tipo": "pppoe" if ppp else "ip",
            "modo": "puente" if "Bridged" in tipo_conexion else "ruta",
            "direccionamiento": None if ppp else (_texto(conexion, "AddressingType") or "DHCP"),
            "vlan": _vlan_de(conexion, marca, prefijo),
            "servicio": servicio,
            "habilitada": _si(conexion, "Enable"),
            "estado": _texto(conexion, "ConnectionStatus"),
            "ip": _texto(conexion, "ExternalIPAddress") if _texto(conexion, "ExternalIPAddress") not in ("", "0.0.0.0") else None,
            "mascara": None if ppp else _texto(conexion, "SubnetMask") or None,
            "puerta_enlace": _texto(conexion, "DefaultGateway") if _texto(conexion, "DefaultGateway") not in ("", "0.0.0.0") else None,
            "dns": _texto(conexion, "DNSServers"),
            "usuario": _texto(conexion, "Username") if ppp else None,
            "nat": _si(conexion, "NATEnabled"),
            "tr069": lleva_tr069(conexion, servicio, ip_gestion),
            "puertos": enlaces.get(ruta, []),
            "dmz": None if not isinstance(dmz, dict) else {
                "habilitada": _si(dmz, "DMZEnable"), "ip": _texto(dmz, "DMZHostIPAddress") or None,
            },
            "puertos_abiertos": [
                {
                    "ruta": f"{ruta}.PortMapping.{n}",
                    "habilitado": _si(regla, "PortMappingEnabled"),
                    "externo": _texto(regla, "ExternalPort"),
                    "interno": _texto(regla, "InternalPort"),
                    "ip": _texto(regla, "InternalClient"),
                    "protocolo": _texto(regla, "PortMappingProtocol"),
                    "descripcion": _texto(regla, "PortMappingDescription"),
                }
                for n, regla in _instancias(conexion.get("PortMapping"))
            ],
        })
    return salida


def _campo_ancho(wlan: Dict[str, Any], marca: str, prefijo: Optional[str]) -> Optional[str]:
    if marca == "huawei":
        return "X_HW_HT20" if "X_HW_HT20" in wlan else None
    return next((c for c in wlan if c.startswith(f"{prefijo}_") and re.search(r"(Band|Channel)Width$", c, re.I)), None)


def _banda_wifi(indice: int, wlan: Dict[str, Any]) -> str:
    """Por la banda que dice la ONU; si no, por el estándar (con "a" es 5 GHz: 11ac, a,n,ac)."""
    banda = next((_texto(wlan, c) for c in wlan
                  if (c.endswith("OperatingFrequencyBand") or c == "X_HW_RFBand") and _texto(wlan, c)), "")
    if banda:
        return "5 GHz" if banda.strip().startswith("5") else "2.4 GHz"
    estandar = (_texto(wlan, "X_HW_Standard") or _texto(wlan, "Standard")).lower().replace("ax", "")
    if estandar:
        return "5 GHz" if "a" in estandar else "2.4 GHz"
    return "5 GHz" if indice > 4 else "2.4 GHz"


CANALES = {"2.4 GHz": list(range(1, 14)), "5 GHz": [36, 40, 44, 48, 52, 56, 60, 64, 149, 153, 157, 161, 165]}


def redes(igd: Dict[str, Any], marca: str, prefijo: Optional[str]) -> List[Dict[str, Any]]:
    salida = []
    for indice, wlan in _instancias(_nodo(igd, WLANS)):
        campo_ancho = _campo_ancho(wlan, marca, prefijo)
        ancho = _texto(wlan, campo_ancho) if campo_ancho else None
        en_uso = _texto(wlan, "PossibleChannels") or _texto(wlan, "ChannelsInUse")
        banda = _banda_wifi(indice, wlan)
        salida.append({
            "indice": indice,
            "ssid": _texto(wlan, "SSID"),
            "banda": banda,
            "habilitada": _si(wlan, "Enable"),
            "oculta": not _si(wlan, "SSIDAdvertisementEnabled") if "SSIDAdvertisementEnabled" in wlan else False,
            "canal": 0 if _si(wlan, "AutoChannelEnable") else int(_texto(wlan, "Channel") or 0),
            "canal_actual": int(_texto(wlan, "Channel") or 0) or None,
            "canales": ([int(c) for c in en_uso.split(",") if c.strip().isdigit()] if "," in en_uso
                        else CANALES[banda]),
            "ancho": ancho,
            "anchos": anchos_huawei(wlan, banda) if marca == "huawei" and campo_ancho else ({ancho: ancho} if ancho else {}),
            "ancho_actual": _texto(wlan, "X_HW_CurrentOperatingChannelBandwidth") or None,
            "seguridad": "abierta" if _texto(wlan, "BeaconType") in ("None", "Basic") else "con contraseña",
        })
    return salida


def lan(igd: Dict[str, Any]) -> Dict[str, Any]:
    host = _nodo(igd, HOST)
    interfaz = next((i for _, i in _instancias(host.get("IPInterface")) if _si(i, "Enable")), {})
    return {
        "ip": _texto(interfaz, "IPInterfaceIPAddress") or _texto(host, "IPRouters"),
        "mascara": _texto(interfaz, "IPInterfaceSubnetMask") or _texto(host, "SubnetMask"),
        "dhcp": _si(host, "DHCPServerEnable"),
        "inicio": _texto(host, "MinAddress"),
        "fin": _texto(host, "MaxAddress"),
        "tiempo": int(_texto(host, "DHCPLeaseTime") or 0) or None,
        "dns": _texto(host, "DNSServers"),
        "reservas": [
            {"mac": _texto(r, "Chaddr"), "ip": _texto(r, "Yiaddr")}
            for _, r in _instancias(host.get("DHCPStaticAddress")) if _texto(r, "Chaddr")
        ],
    }


def puertos_disponibles(igd: Dict[str, Any]) -> List[str]:
    lan_eth = [f"LAN{i}" for i, _ in _instancias(_nodo(igd, LAN_ETH))] or [f"LAN{i}" for i in range(1, 5)]
    return lan_eth + [f"SSID{i}" for i, _ in _instancias(_nodo(igd, WLANS))]


def configuracion(documento: Dict[str, Any]) -> Dict[str, Any]:
    igd = documento.get(IGD) or {}
    marca, prefijo = marca_de(igd)
    return {
        "marca": marca,
        "ultimo_reporte": documento.get("_lastInform"),
        "wans": wans(igd, marca, prefijo, ip_de_gestion(documento)),
        "wifi": redes(igd, marca, prefijo),
        "lan": lan(igd),
        "puertos": puertos_disponibles(igd),
    }


# ------------------------------------------------------------------ escritura (pura)
def valores_wan(conexion: Dict[str, Any], ruta: str, datos: Dict[str, Any], marca: str, prefijo: Optional[str],
                es_tr069: bool, nueva: bool = False) -> List[list]:
    """setParameterValues para editar (o terminar de crear) una WAN."""
    ppp = ".WANPPPConnection." in ruta
    valores: List[list] = []

    def poner(campo: str, valor: Any, tipo: str = "xsd:string"):
        valores.append([f"{ruta}.{campo}", valor, tipo])

    if es_tr069:
        actual_modo = "puente" if "Bridged" in _texto(conexion, "ConnectionType") else "ruta"
        if datos.get("habilitada") is False:
            raise ValueError("Esa WAN lleva el TR-069: si se apaga, el módem se queda sin control remoto.")
        if datos.get("modo") and datos["modo"] != actual_modo:
            raise ValueError("Esa WAN lleva el TR-069: en puente el módem se queda sin control remoto.")
        if "vlan" in datos and _vlan(datos["vlan"]) != _vlan_de(conexion, marca, prefijo):
            raise ValueError("Esa WAN lleva el TR-069: con otra VLAN el módem se queda sin control remoto.")
        if datos.get("servicio") and "TR069" not in datos["servicio"].upper():
            raise ValueError("Esa WAN lleva el TR-069: su servicio tiene que seguir incluyéndolo.")

    if "modo" in datos:
        if datos["modo"] not in ("ruta", "puente"):
            raise ValueError("El modo es ruta o puente.")
        poner("ConnectionType", ("IP_Routed" if datos["modo"] == "ruta" else "PPPoE_Bridged" if ppp else "IP_Bridged"))
        if datos["modo"] == "ruta" and "nat" not in datos and nueva:
            poner("NATEnabled", True, "xsd:boolean")
    if "vlan" in datos:
        vlan = _vlan(datos["vlan"])
        if marca == "huawei":
            poner("X_HW_VLAN", vlan, "xsd:unsignedInt")
        else:
            poner(f"{prefijo}_VLANEnable", True, "xsd:boolean")
            poner(f"{prefijo}_VLANID", vlan, "xsd:unsignedInt")
    if datos.get("servicio"):
        servicio = datos["servicio"].strip().upper()
        if not re.fullmatch(r"[A-Z0-9_]{2,40}", servicio):
            raise ValueError("Servicio no válido (ej. INTERNET, TR069_INTERNET, VOIP).")
        poner("X_HW_SERVICELIST" if marca == "huawei" else f"{prefijo}_ServiceList", servicio)
    if datos.get("nombre") is not None and str(datos["nombre"]).strip():
        poner("Name", str(datos["nombre"]).strip()[:32])
    if "nat" in datos:
        poner("NATEnabled", bool(datos["nat"]), "xsd:boolean")
    if ppp:
        if datos.get("usuario") is not None:
            usuario = str(datos["usuario"]).strip()
            if not usuario or len(usuario) > 64:
                raise ValueError("El usuario PPPoE no puede ir vacío.")
            poner("Username", usuario)
        if datos.get("clave"):
            poner("Password", str(datos["clave"])[:64])
    else:
        if datos.get("direccionamiento"):
            if datos["direccionamiento"] not in ("DHCP", "Static"):
                raise ValueError("La IP es por DHCP o fija.")
            poner("AddressingType", datos["direccionamiento"])
            if datos["direccionamiento"] == "Static":
                poner("ExternalIPAddress", _ip(datos.get("ip"), "IP de la WAN"))
                poner("SubnetMask", _ip(datos.get("mascara"), "Máscara"))
                poner("DefaultGateway", _ip(datos.get("puerta_enlace"), "Puerta de enlace"))
    if datos.get("dns") is not None and str(datos["dns"]).strip():
        poner("DNSServers", ",".join(_ip(d, "DNS") for d in str(datos["dns"]).split(",") if d.strip()))
    if "habilitada" in datos:
        poner("Enable", bool(datos["habilitada"]), "xsd:boolean")
    return valores


def valores_wifi(indice: int, wlan: Dict[str, Any], datos: Dict[str, Any], marca: str, prefijo: Optional[str]) -> List[list]:
    base = f"{WLANS}.{indice}"
    valores: List[list] = []
    if datos.get("ssid") is not None:
        ssid = str(datos["ssid"]).strip()
        if not 1 <= len(ssid) <= 32 or not re.fullmatch(r"[\x20-\x7e]+", ssid):
            raise ValueError("El nombre de la red va de 1 a 32 caracteres, sin acentos ni ñ.")
        valores.append([f"{base}.SSID", ssid, "xsd:string"])
    if "oculta" in datos:
        valores.append([f"{base}.SSIDAdvertisementEnabled", not datos["oculta"], "xsd:boolean"])
    if "canal" in datos:
        canal = int(datos["canal"] or 0)
        if canal:
            permitidos = [int(c) for c in (_texto(wlan, "PossibleChannels") or _texto(wlan, "ChannelsInUse")).split(",")
                          if c.strip().isdigit()]
            if len(permitidos) > 1 and canal not in permitidos:
                raise ValueError(f"El canal {canal} no es de esta banda.")
            valores.append([f"{base}.AutoChannelEnable", False, "xsd:boolean"])
            valores.append([f"{base}.Channel", canal, "xsd:unsignedInt"])
        else:
            valores.append([f"{base}.AutoChannelEnable", True, "xsd:boolean"])
    if datos.get("ancho") is not None:
        campo = _campo_ancho(wlan, marca, prefijo)
        if not campo:
            raise ValueError("Este módem no deja cambiar el ancho del canal por TR-069.")
        if marca == "huawei" and str(datos["ancho"]) not in ANCHOS_HUAWEI:
            raise ValueError("Ancho no válido.")
        valores.append([f"{base}.{campo}", int(datos["ancho"]) if marca == "huawei" else str(datos["ancho"]),
                        "xsd:unsignedInt" if marca == "huawei" else "xsd:string"])
    if "habilitada" in datos:
        valores.append([f"{base}.Enable", bool(datos["habilitada"]), "xsd:boolean"])
    return valores


def valores_lan(datos: Dict[str, Any], interfaz: str) -> List[list]:
    ip = ipaddress.IPv4Address(_ip(datos.get("ip"), "IP del módem"))
    mascara = _ip(datos.get("mascara") or "255.255.255.0", "Máscara")
    try:
        red = ipaddress.IPv4Network(f"{ip}/{mascara}", strict=False)
    except ValueError as error:
        raise ValueError("Máscara no válida.") from error
    valores = [
        [f"{interfaz}.IPInterfaceIPAddress", str(ip), "xsd:string"],
        [f"{interfaz}.IPInterfaceSubnetMask", mascara, "xsd:string"],
        [f"{HOST}.SubnetMask", mascara, "xsd:string"],
        [f"{HOST}.IPRouters", str(ip), "xsd:string"],
    ]
    if "dhcp" in datos:
        valores.append([f"{HOST}.DHCPServerEnable", bool(datos["dhcp"]), "xsd:boolean"])
    if datos.get("dhcp", True):
        inicio = ipaddress.IPv4Address(_ip(datos.get("inicio"), "Inicio del DHCP"))
        fin = ipaddress.IPv4Address(_ip(datos.get("fin"), "Fin del DHCP"))
        if inicio not in red or fin not in red or inicio > fin:
            raise ValueError(f"El rango del DHCP tiene que estar dentro de {red} y el inicio antes del fin.")
        if inicio <= ip <= fin:
            raise ValueError("La IP del módem no puede quedar dentro del rango del DHCP.")
        valores += [[f"{HOST}.MinAddress", str(inicio), "xsd:string"], [f"{HOST}.MaxAddress", str(fin), "xsd:string"]]
        if datos.get("tiempo"):
            tiempo = int(datos["tiempo"])
            if not 60 <= tiempo <= 604800:
                raise ValueError("El tiempo del DHCP va de 1 minuto a 7 días.")
            valores.append([f"{HOST}.DHCPLeaseTime", tiempo, "xsd:int"])
        dns = str(datos.get("dns") or "").strip() or str(ip)
        valores.append([f"{HOST}.DNSServers", ",".join(_ip(d, "DNS") for d in dns.split(",") if d.strip()), "xsd:string"])
    return valores


def enlaces_nuevos(actuales: Dict[str, List[str]], wan: str, puertos: List[str]) -> Dict[str, List[str]]:
    """Los puertos van a esta WAN y salen de las demás (un puerto solo puede ir por una)."""
    salida = {w: [p for p in ps if p not in puertos] for w, ps in actuales.items() if w != wan}
    salida[wan] = sorted(set(puertos), key=_orden_puerto)
    return salida


def solo_cambios(actual: Dict[str, Any], datos: Dict[str, Any]) -> Dict[str, Any]:
    """Lo que de verdad cambia: reescribir el mismo modo o VLAN reconecta la WAN sin necesidad."""
    salida = {}
    for campo, valor in datos.items():
        if campo == "clave":
            if valor:
                salida[campo] = valor
        elif campo == "dns":
            if str(valor or "").replace(" ", "") != str(actual.get("dns") or "").replace(" ", ""):
                salida[campo] = valor
        elif campo == "puertos":
            if valor is not None and sorted(valor) != sorted(actual.get("puertos") or []):
                salida[campo] = valor
        elif campo == "dmz":
            if valor is not None and valor != actual.get("dmz"):
                salida[campo] = valor
        elif campo in ("ip", "mascara", "puerta_enlace"):
            salida[campo] = valor
        elif valor != actual.get(campo):
            salida[campo] = valor
    if "direccionamiento" not in salida and salida.keys() & {"ip", "mascara", "puerta_enlace"}:
        if datos.get("direccionamiento") == "Static":
            salida["direccionamiento"] = "Static"
    if "direccionamiento" not in salida:
        for campo in ("ip", "mascara", "puerta_enlace"):
            salida.pop(campo, None)
    return salida


# ------------------------------------------------------------------ lista de equipos
def serial_reportado(documento: Dict[str, Any]) -> str:
    return str((documento.get("_deviceId") or {}).get("_SerialNumber") or "").upper()


def ficha_equipo(documento: Dict[str, Any]) -> Dict[str, Any]:
    ident = documento.get("_deviceId") or {}
    igd = documento.get(IGD) or {}
    serie = serial_reportado(documento)
    if re.fullmatch(r"[0-9A-F]{16}", serie):
        try:
            legible = bytes.fromhex(serie[:8]).decode("ascii") + serie[8:]
        except (ValueError, UnicodeDecodeError):
            legible = serie
    else:
        legible = serie
    return {
        "id": documento.get("_id"),
        "fabricante": ident.get("_Manufacturer") or "",
        "modelo": ident.get("_ProductClass") or "",
        "serie": legible,
        "firmware": _texto(_nodo(igd, f"{IGD}.DeviceInfo"), "SoftwareVersion") or None,
        "ip": ip_de_gestion(documento),
        "ultimo_reporte": documento.get("_lastInform"),
        "ultimo_arranque": documento.get("_lastBoot"),
    }


# ------------------------------------------------------------------ servicio
class ConfigOnuService:
    def __init__(self, db: AsyncSession, nbi: Optional[str] = None, transporte=None):
        self.red = RedClienteService(db, nbi)
        self.transporte = transporte

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.red.nbi, timeout=ESPERA_ONU_MS / 1000 + 20, transport=self.transporte)

    async def _existe(self, http: httpx.AsyncClient, device_id: str) -> None:
        respuesta = await http.get("/devices", params={"query": json.dumps({"_id": device_id}), "projection": "_id"})
        respuesta.raise_for_status()
        if not respuesta.json():
            raise LookupError("Ese equipo no está en el ACS.")

    async def _documento(self, http: httpx.AsyncClient, device_id: str) -> Dict[str, Any]:
        respuesta = await http.get("/devices", params={"query": json.dumps({"_id": device_id})})
        respuesta.raise_for_status()
        return (respuesta.json() or [{}])[0]

    async def _refrescar(self, http: httpx.AsyncClient, device_id: str, objetos: List[str]) -> None:
        for objeto in objetos:
            await self.red._obligada(http, device_id, {"name": "refreshObject", "objectName": objeto})

    async def _releer(self, http: httpx.AsyncClient, device_id: str, partes: Optional[List[str]] = None) -> Dict[str, Any]:
        """Le pregunta a la ONU lo que se va a mostrar o cambiar y devuelve el documento."""
        documento = await self._documento(http, device_id)
        igd = documento.get(IGD) or {}
        marca, prefijo = marca_de(igd)
        partes = partes or ["wan", "wifi", "lan"]
        objetos = []
        if "wan" in partes:
            objetos.append(CONEXIONES)
            objetos.append(f"{IGD}.{prefijo}_PortBinding" if marca == "zte" else RUTAS)
        if "wifi" in partes:
            objetos.append(WLANS)
        if "lan" in partes:
            objetos.append(HOST)
        try:
            await self._refrescar(http, device_id, objetos)
        except ValueError:
            # Una ruta que este firmware no tiene (otro modelo): se lee lo demás.
            for objeto in objetos:
                try:
                    await self._refrescar(http, device_id, [objeto])
                except ValueError as error:
                    logger.info("ONU %s no leyó %s: %s", device_id, objeto, error)
        return await self._documento(http, device_id)

    async def leer(self, device_id: str, refrescar: bool = True) -> Dict[str, Any]:
        async with self._http() as http:
            await self._existe(http, device_id)
            async with _candado(device_id):
                al_dia = True
                documento = await self._documento(http, device_id)
                if refrescar or not conexiones_wan(documento.get(IGD) or {}):
                    try:
                        documento = await self._releer(http, device_id)
                    except ValueError:
                        al_dia = False
        return {**configuracion(documento), "al_dia": al_dia}

    async def _aplicar(self, http, device_id: str, valores: List[list]) -> None:
        if valores:
            await self.red._obligada(http, device_id, {"name": "setParameterValues", "parameterValues": valores})

    async def _con_onu(self, device_id: str, usuario, accion: str, partes: List[str], trabajo) -> Dict[str, Any]:
        """Lee lo actual, hace el cambio, vuelve a leer y lo anota en la bitácora."""
        async with self._http() as http:
            await self._existe(http, device_id)
            async with _candado(device_id):
                documento = await self._releer(http, device_id, partes)
                detalle = await trabajo(http, device_id, documento)
                documento = await self._releer(http, device_id, partes)
        await self._bitacora(usuario, accion, device_id, detalle)
        return {**configuracion(documento), "al_dia": True}

    async def _bitacora(self, usuario, accion: str, device_id: str, detalle: str) -> None:
        db = self.red.db
        if db is None:
            return
        db.add(LogActividadModel(
            usuario_id=getattr(usuario, "id", None), usuario_nombre=getattr(usuario, "usuario", None),
            accion=f"TR-069: {accion}", metodo="POST", ruta=f"/api/tr069/equipos/{device_id}",
            estado_http=200, detalle=f"{device_id} · {detalle}"[:1000],
        ))
        await db.commit()

    # ------------------------------------------------------------ WAN
    async def guardar_wan(self, device_id: str, ruta: str, datos: Dict[str, Any], usuario=None) -> Dict[str, Any]:
        if not RE_RUTA_WAN.match(ruta or ""):
            raise ValueError("WAN no válida.")

        async def trabajo(http, device_id, documento):
            igd = documento.get(IGD) or {}
            conexion = _nodo(igd, ruta)
            if not conexion:
                raise LookupError("Esa WAN ya no existe en el módem.")
            marca, prefijo = marca_de(igd)
            es_tr069 = lleva_tr069(conexion, _servicio(conexion, marca, prefijo), ip_de_gestion(documento))
            actual = next(w for w in wans(igd, marca, prefijo, ip_de_gestion(documento)) if w["ruta"] == ruta)
            cambios = solo_cambios(actual, datos)
            if not cambios:
                return f"WAN {actual['nombre']} · sin cambios"
            await self._aplicar(http, device_id, valores_wan(conexion, ruta, cambios, marca, prefijo, es_tr069))
            dmz = cambios.get("dmz")
            if isinstance(dmz, dict) and isinstance(conexion.get("X_HW_DMZ"), dict):
                valores = [[f"{ruta}.X_HW_DMZ.DMZEnable", bool(dmz.get("habilitada")), "xsd:boolean"]]
                if dmz.get("habilitada"):
                    valores.insert(0, [f"{ruta}.X_HW_DMZ.DMZHostIPAddress", _ip(dmz.get("ip"), "IP de la DMZ"), "xsd:string"])
                await self._aplicar(http, device_id, valores)
            if cambios.get("puertos") is not None:
                await self._poner_enlaces(http, device_id, igd, ruta, list(cambios["puertos"]))
            return f"WAN {actual['nombre']} · " + ", ".join(sorted(k for k in cambios if k != "clave"))

        return await self._con_onu(device_id, usuario, "Editar WAN", ["wan"], trabajo)

    async def agregar_wan(self, device_id: str, datos: Dict[str, Any], usuario=None) -> Dict[str, Any]:
        tipo = datos.get("tipo")
        if tipo not in ("pppoe", "ip"):
            raise ValueError("La WAN nueva es PPPoE o IP.")
        _vlan(datos.get("vlan"))
        if tipo == "pppoe" and datos.get("modo", "ruta") == "ruta" and not (datos.get("usuario") and datos.get("clave")):
            raise ValueError("Una WAN PPPoE en ruta necesita usuario y contraseña.")

        async def trabajo(http, device_id, documento):
            igd = documento.get(IGD) or {}
            marca, prefijo = marca_de(igd)
            objeto = "WANPPPConnection" if tipo == "pppoe" else "WANIPConnection"
            if marca == "huawei":
                dispositivo = await self.red.agregar_instancia(http, device_id, CONEXIONES)
            else:
                dispositivo = next((i for i, _ in _instancias(_nodo(igd, CONEXIONES))), None)
                if dispositivo is None:
                    raise ValueError("El módem no tiene dónde crear la WAN.")
            try:
                n = await self.red.agregar_instancia(http, device_id, f"{CONEXIONES}.{dispositivo}.{objeto}")
            except ValueError:
                if marca == "huawei":
                    await self._borrar_silencioso(http, device_id, f"{CONEXIONES}.{dispositivo}")
                raise
            ruta = f"{CONEXIONES}.{dispositivo}.{objeto}.{n}"
            completos = {"modo": "ruta", "servicio": "INTERNET", **datos, "habilitada": True}
            if tipo == "ip" and completos["modo"] == "ruta":
                completos.setdefault("direccionamiento", "DHCP")
            completos.pop("tipo", None)
            puertos = completos.pop("puertos", None)
            try:
                await self._aplicar(http, device_id, valores_wan({}, ruta, completos, marca, prefijo, False, nueva=True))
            except ValueError:
                await self._borrar_silencioso(http, device_id, ruta if marca == "zte" else f"{CONEXIONES}.{dispositivo}")
                raise
            if puertos:
                igd = (await self._releer(http, device_id, ["wan"])).get(IGD) or {}
                await self._poner_enlaces(http, device_id, igd, ruta, list(puertos))
            return f"WAN nueva {tipo} {completos['modo']} VLAN {completos.get('vlan')} ({ruta})"

        return await self._con_onu(device_id, usuario, "Agregar WAN", ["wan"], trabajo)

    async def borrar_wan(self, device_id: str, ruta: str, usuario=None) -> Dict[str, Any]:
        if not RE_RUTA_WAN.match(ruta or ""):
            raise ValueError("WAN no válida.")

        async def trabajo(http, device_id, documento):
            igd = documento.get(IGD) or {}
            conexion = _nodo(igd, ruta)
            if not conexion:
                raise LookupError("Esa WAN ya no existe en el módem.")
            marca, prefijo = marca_de(igd)
            if lleva_tr069(conexion, _servicio(conexion, marca, prefijo), ip_de_gestion(documento)):
                raise ValueError("Esa WAN lleva el TR-069: si se borra, el módem se queda sin control remoto.")
            # Primero se suelta de sus puertos, para no dejar enlaces apuntando a nada.
            if enlaces_actuales(igd, marca, prefijo).get(ruta):
                await self._poner_enlaces(http, device_id, igd, ruta, [])
                igd = (await self._releer(http, device_id, ["wan"])).get(IGD) or {}
            if marca == "zte":
                for n, enlace in _instancias(igd.get(f"{prefijo}_PortBinding")):
                    if _texto(enlace, "WANInterface").rstrip(".") == ruta:
                        await self.red._obligada(http, device_id, {"name": "deleteObject",
                                                                     "objectName": f"{IGD}.{prefijo}_PortBinding.{n}"})
            else:
                for n, regla in _instancias(_nodo(igd, RUTAS)):
                    if _texto(regla, "WanName") == nombre_wan_huawei(ruta):
                        await self.red._obligada(http, device_id, {"name": "deleteObject", "objectName": f"{RUTAS}.{n}"})
            dispositivo = ruta.rsplit(".", 2)[0]
            hermanas = [r for r, _ in conexiones_wan(igd) if r.startswith(dispositivo + ".") and r != ruta]
            objetivo = dispositivo if marca == "huawei" and not hermanas else ruta
            await self.red._obligada(http, device_id, {"name": "deleteObject", "objectName": objetivo})
            return f"WAN borrada {_texto(conexion, 'Name') or ruta}"

        return await self._con_onu(device_id, usuario, "Borrar WAN", ["wan"], trabajo)

    async def _borrar_silencioso(self, http, device_id: str, objeto: str) -> None:
        try:
            await self.red._obligada(http, device_id, {"name": "deleteObject", "objectName": objeto})
        except (ValueError, httpx.HTTPError) as error:
            logger.warning("No se pudo deshacer %s en %s: %s", objeto, device_id, error)

    # ------------------------------------------------------------ enlaces
    async def guardar_enlaces(self, device_id: str, ruta: str, puertos: List[str], usuario=None) -> Dict[str, Any]:
        if not RE_RUTA_WAN.match(ruta or ""):
            raise ValueError("WAN no válida.")

        async def trabajo(http, device_id, documento):
            await self._poner_enlaces(http, device_id, documento.get(IGD) or {}, ruta, puertos)
            return f"Enlace {ruta}: {','.join(puertos) or 'ninguno'}"

        return await self._con_onu(device_id, usuario, "Enlace de puertos", ["wan"], trabajo)

    async def _poner_enlaces(self, http, device_id: str, igd: Dict[str, Any], ruta: str, puertos: List[str]) -> None:
        puertos = [str(p).strip().upper() for p in puertos]
        disponibles = set(puertos_disponibles(igd))
        malos = [p for p in puertos if not RE_PUERTO.match(p) or p not in disponibles]
        if malos:
            raise ValueError(f"Puertos que este módem no tiene: {', '.join(malos)}.")
        if not _nodo(igd, ruta):
            raise LookupError("Esa WAN ya no existe en el módem.")
        marca, prefijo = marca_de(igd)
        actuales = enlaces_actuales(igd, marca, prefijo)
        nuevos = enlaces_nuevos(actuales, ruta, puertos)
        cambiados = {w: p for w, p in nuevos.items() if p != actuales.get(w, [])}
        if marca == "zte":
            await self._enlaces_zte(http, device_id, igd, prefijo, cambiados)
        else:
            await self._enlaces_huawei(http, device_id, igd, cambiados)

    async def _enlaces_zte(self, http, device_id, igd, prefijo, cambiados: Dict[str, List[str]]) -> None:
        base = f"{IGD}.{prefijo}_PortBinding"
        existentes = {_texto(e, "WANInterface").rstrip("."): n for n, e in _instancias(igd.get(f"{prefijo}_PortBinding"))}
        for wan, puertos in cambiados.items():
            n = existentes.get(wan)
            if n is None and not puertos:
                continue
            if n is None:
                n = await self.red.agregar_instancia(http, device_id, base)
            if not puertos:
                await self.red._obligada(http, device_id, {"name": "deleteObject", "objectName": f"{base}.{n}"})
                continue
            await self._aplicar(http, device_id, [
                [f"{base}.{n}.WANInterface", wan, "xsd:string"],
                [f"{base}.{n}.LANInterface", ",".join(ruta_de_puerto(p) for p in puertos), "xsd:string"],
            ])

    async def _enlaces_huawei(self, http, device_id, igd, cambiados: Dict[str, List[str]]) -> None:
        reglas = {_texto(r, "WanName"): n for n, r in _instancias(_nodo(igd, RUTAS))
                  if _texto(r, "PolicyRouteType") == "SourcePhyPort"}
        # Primero se quitan puertos (para que ninguno quede en dos WAN a la vez) y luego se ponen.
        for wan, puertos in sorted(cambiados.items(), key=lambda p: len(p[1])):
            conexion = _nodo(igd, wan)
            enlace = conexion.get("X_HW_LANBIND") or {}
            valores = []
            for campo in enlace:
                m = re.fullmatch(r"(Lan|SSID)(\d+)Enable", campo)
                if m:
                    valores.append([f"{wan}.X_HW_LANBIND.{campo}", 1 if f"{m.group(1).upper()}{m.group(2)}" in puertos else 0,
                                    "xsd:unsignedInt"])
            await self._aplicar(http, device_id, valores)
            nombre = nombre_wan_huawei(wan)
            n = reglas.get(nombre)
            if not puertos:
                if n is not None:
                    await self.red._obligada(http, device_id, {"name": "deleteObject", "objectName": f"{RUTAS}.{n}"})
                continue
            if n is None:
                n = await self.red.agregar_instancia(http, device_id, RUTAS)
                await self._aplicar(http, device_id, [
                    [f"{RUTAS}.{n}.PolicyRouteType", "SourcePhyPort", "xsd:string"],
                    [f"{RUTAS}.{n}.WanName", nombre, "xsd:string"],
                ])
            await self._aplicar(http, device_id, [[f"{RUTAS}.{n}.PhyPortName", ",".join(puertos), "xsd:string"]])

    # ------------------------------------------------------------ WiFi
    async def guardar_wifi(self, device_id: str, indice: int, datos: Dict[str, Any], usuario=None) -> Dict[str, Any]:
        async def trabajo(http, device_id, documento):
            igd = documento.get(IGD) or {}
            wlan = _nodo(igd, f"{WLANS}.{indice}")
            if not wlan:
                raise LookupError("Esa red WiFi no existe en el módem.")
            marca, prefijo = marca_de(igd)
            actual = next(r for r in redes(igd, marca, prefijo) if r["indice"] == indice)
            cambios = {k: v for k, v in datos.items() if v is not None and v != actual.get(k)}
            await self._aplicar(http, device_id, valores_wifi(indice, wlan, cambios, marca, prefijo))
            return f"WiFi {indice} ({_texto(wlan, 'SSID')}) · " + (", ".join(sorted(cambios)) or "sin cambios")

        return await self._con_onu(device_id, usuario, "Editar WiFi", ["wifi"], trabajo)

    # ------------------------------------------------------------ LAN
    async def guardar_lan(self, device_id: str, datos: Dict[str, Any], usuario=None) -> Dict[str, Any]:
        async def trabajo(http, device_id, documento):
            host = _nodo(documento.get(IGD) or {}, HOST)
            n = next((n for n, i in _instancias(host.get("IPInterface")) if _si(i, "Enable")), 1)
            await self._aplicar(http, device_id, valores_lan(datos, f"{HOST}.IPInterface.{n}"))
            return f"LAN {datos.get('ip')} · DHCP {datos.get('inicio')}-{datos.get('fin')}"

        return await self._con_onu(device_id, usuario, "Editar LAN", ["lan"], trabajo)

    # ------------------------------------------------------------ puertos abiertos (NAT)
    async def abrir_puerto(self, device_id: str, datos: Dict[str, Any], usuario=None) -> Dict[str, Any]:
        wan = datos.get("wan") or ""
        if not RE_RUTA_WAN.match(wan):
            raise ValueError("Elige la WAN por donde entra.")
        externo = _puerto_tcp(datos.get("externo"), "Puerto externo")
        interno = _puerto_tcp(datos.get("interno") or externo, "Puerto interno")
        ip = _ip(datos.get("ip"), "IP del equipo")
        protocolo = str(datos.get("protocolo") or "TCP").upper()
        protocolos = PROTOCOLOS if protocolo == "AMBOS" else (protocolo,)
        if any(p not in PROTOCOLOS for p in protocolos):
            raise ValueError("El protocolo es TCP, UDP o ambos.")
        descripcion = re.sub(r"[^\w .-]", "", str(datos.get("descripcion") or f"Puerto {externo}"))[:32]

        async def trabajo(http, device_id, documento):
            conexion = _nodo(documento.get(IGD) or {}, wan)
            if not conexion:
                raise LookupError("Esa WAN ya no existe en el módem.")
            if "Bridged" in _texto(conexion, "ConnectionType"):
                raise ValueError("Esa WAN va en puente: no se le pueden abrir puertos.")
            for p in protocolos:
                n = await self.red.agregar_instancia(http, device_id, f"{wan}.PortMapping")
                regla = f"{wan}.PortMapping.{n}"
                try:
                    await self._aplicar(http, device_id, [
                        [f"{regla}.ExternalPort", externo, "xsd:unsignedInt"],
                        [f"{regla}.InternalPort", interno, "xsd:unsignedInt"],
                        [f"{regla}.InternalClient", ip, "xsd:string"],
                        [f"{regla}.PortMappingProtocol", p, "xsd:string"],
                        [f"{regla}.PortMappingDescription", descripcion, "xsd:string"],
                        [f"{regla}.PortMappingEnabled", True, "xsd:boolean"],
                    ])
                except ValueError:
                    await self._borrar_silencioso(http, device_id, regla)
                    raise
            return f"Abrir puerto {externo}→{ip}:{interno} {protocolo}"

        return await self._con_onu(device_id, usuario, "Abrir puerto", ["wan"], trabajo)

    async def cerrar_puerto(self, device_id: str, ruta: str, usuario=None) -> Dict[str, Any]:
        m = re.match(r"^(.+)\.PortMapping\.(\d+)$", ruta or "")
        if not m or not RE_RUTA_WAN.match(m.group(1)):
            raise ValueError("Regla no válida.")

        async def trabajo(http, device_id, documento):
            regla = _nodo(documento.get(IGD) or {}, ruta)
            if not regla:
                raise LookupError("Esa regla ya no existe en el módem.")
            await self.red._obligada(http, device_id, {"name": "deleteObject", "objectName": ruta})
            return f"Cerrar puerto {_texto(regla, 'ExternalPort')}→{_texto(regla, 'InternalClient')}"

        return await self._con_onu(device_id, usuario, "Cerrar puerto", ["wan"], trabajo)
