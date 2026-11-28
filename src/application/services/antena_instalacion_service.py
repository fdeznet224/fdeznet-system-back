"""Antena de un cliente de radio: la plantilla que aplica la app del técnico y
lo que se mide al instalarla.

La antena queda en modo router (airOS 6 "Router"): el PPPoE del cliente va
por el radio etiquetado en la VLAN de clientes (ath0.10, como la antena de
Ubelia Lopez que dejó el dueño funcionando el 27 nov), la administración en la VLAN <vlan> del radio con la IP del pool
de antenas, y la LAN (cable) reparte una sola IP al Mercusys, que queda de
fábrica; la DMZ le manda todo lo que llega al PPPoE (su acceso remoto 8080),
menos los puertos de la propia antena. Bloque sacado de una LiteBeam M5
v6.3.11 configurada desde su web (27 nov 2026).

El usuario y la clave de la empresa van como hash ($1$..., el de airOS), nunca
en claro: configuracion antena_usuario, antena_clave_hash, antena_vlan,
antena_lan_ip (192.168.20.1) y antena_ruta_vpn (10.8.0.0/24, por donde el
sistema llega a la antena).
"""

import ipaddress
import re
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.services.pool_antenas_service import PoolAntenasService
from src.infrastructure.models import ClienteModel, ConfiguracionModel, EquipoRedModel

CLAVES = (
    "antena_usuario", "antena_clave_hash", "antena_vlan", "antena_vlan_cliente", "antena_lan_ip", "antena_ruta_vpn",
    "antena_pppoe_instalacion_usuario", "antena_pppoe_instalacion_clave", "antena_pais", "antena_potencia",
    "antena_clave",
)
NOMBRE_MAXIMO = 63


def _mhz(texto: Any) -> Optional[int]:
    numero = re.search(r"\d+(?:\.\d+)?", str(texto or ""))
    if not numero:
        return None
    valor = float(numero.group())
    return round(valor * 1000) if valor < 100 else round(valor)


def nombre_de_equipo(nombre: str) -> str:
    """Nombre del dispositivo en airOS: el del cliente, sin saltos ni '=' (rompen system.cfg)."""
    limpio = re.sub(r"[\r\n=]+", " ", nombre or "").strip()
    return re.sub(r"\s+", " ", limpio)[:NOMBRE_MAXIMO] or "Cliente"


async def config_antenas(db: AsyncSession) -> Dict[str, str]:
    filas = (await db.execute(select(ConfiguracionModel).where(ConfiguracionModel.clave.in_(CLAVES)))).scalars().all()
    return {f.clave: (f.valor or "").strip() for f in filas}


# Lo que la plantilla reemplaza completo en la configuración de la antena.
PREFIJOS_DE_RED = (
    "netmode", "vlan.", "netconf.", "bridge.", "dhcpc.", "iptables.", "ppp.", "route.", "route6.",
    "dnsmasq.", "dhcpd.", "ebtables.", "upnpd.", "unms.",
)
# Puertos de la antena que la DMZ no manda al Mercusys.
EXCEPCIONES_DMZ = (("8", "ICMP"), ("9", "UDP"), ("18888", "TCP"), ("80", "TCP"), ("22", "TCP"), ("443", "TCP"), ("10001", "UDP"))


def bloque_router(
    *, vlan: int, ip: Optional[str], vlan_cliente: int = 10, mascara: Optional[str], gateway: Optional[str], lan_ip: str,
    pppoe_usuario: str, pppoe_clave: str, ruta_vpn: Optional[ipaddress.IPv4Network], nombre: str,
    usuario: Optional[str], clave_hash: Optional[str],
) -> Dict[str, str]:
    """Claves de system.cfg del modo router (en el orden de airOS)."""
    lan = ipaddress.ip_interface(f"{lan_ip}/24")
    mercusys = str(lan.network.network_address + 2)
    c: Dict[str, str] = {}

    def interfaz(n: int, dev: str, rol: str, ip_: str, mascara_: str, promisc: bool) -> None:
        c.update({
            f"netconf.{n}.status": "enabled", f"netconf.{n}.devname": dev, f"netconf.{n}.mtu": "1500",
            f"netconf.{n}.role": rol, f"netconf.{n}.autoip.status": "disabled",
            f"netconf.{n}.hwaddr.status": "disabled", f"netconf.{n}.hwaddr.mac": "",
            f"netconf.{n}.ip": ip_, f"netconf.{n}.netmask": mascara_, f"netconf.{n}.up": "enabled",
        })
        if promisc:
            c[f"netconf.{n}.promisc"] = "enabled"

    # El PPPoE del cliente va etiquetado en la VLAN de clientes (como lo espera
    # el MikroTik en vlan-CLIENTES); la administración en la suya.
    # Sin IP de administración (antena de instalación, aún sin cliente) no se
    # crea la VLAN de administración ni la ruta a la VPN.
    con_admin = bool(ip and mascara)
    wan = f"ath0.{vlan_cliente}"
    vlans = ([(vlan, "Administracion")] if con_admin else []) + [(vlan_cliente, "Cliente")]
    c.update({"netmode": "router", "vlan.status": "enabled"})
    for n, (vid, comentario) in enumerate(vlans, start=1):
        c.update({f"vlan.{n}.status": "enabled", f"vlan.{n}.devname": "ath0", f"vlan.{n}.id": str(vid), f"vlan.{n}.comment": comentario})
    c["netconf.status"] = "enabled"
    interfaz(1, wan, "wan", "0.0.0.0", "255.255.255.255", True)
    interfaz(2, "eth0", "lan", str(lan.ip), str(lan.netmask), True)
    c.update({"netconf.2.ip6.1.status": "disabled", "netconf.2.ip6.1.addr": "", "netconf.2.ip6.1.netmask": "64"})
    if con_admin:
        interfaz(3, f"ath0.{vlan}", "mlan", ip, mascara, False)
    # El radio solo, encendido y sin rol: debajo de las VLAN.
    r = 4 if con_admin else 3
    c.update({f"netconf.{r}.status": "enabled", f"netconf.{r}.devname": "ath0", f"netconf.{r}.mtu": "1500", f"netconf.{r}.role": "",
              f"netconf.{r}.autoip.status": "disabled", f"netconf.{r}.hwaddr.status": "disabled", f"netconf.{r}.hwaddr.mac": "",
              f"netconf.{r}.up": "enabled"})
    c.update({
        "bridge.status": "disabled",
        "dhcpc.status": "enabled", "dhcpc.1.status": "disabled", "dhcpc.1.devname": wan,
        "dhcpc.1.fallback": "192.168.10.1", "dhcpc.1.fallback_netmask": "255.255.255.0",
        "iptables.status": "enabled", "iptables.sys.portfw.status": "disabled",
        "iptables.sys.masq.status": "enabled", "iptables.sys.masq.1.devname": "ppp+", "iptables.sys.masq.1.status": "enabled",
    })
    for i, (puerto, proto) in enumerate(EXCEPCIONES_DMZ, start=1):
        c.update({f"iptables.sys.dmz.1.except.{i}.port": puerto, f"iptables.sys.dmz.1.except.{i}.proto": proto,
                  f"iptables.sys.dmz.1.except.{i}.status": "enabled"})
    c.update({
        "iptables.sys.dmz.1.except.status": "enabled", "iptables.sys.dmz.1.devname": "ppp+",
        "iptables.sys.dmz.1.status": "enabled", "iptables.sys.dmz.1.host": mercusys, "iptables.sys.dmz.status": "enabled",
        "iptables.sys.mgmt.1.devname": "ppp+", "iptables.sys.mgmt.1.status": "enabled", "iptables.sys.mgmt.status": "enabled",
        "iptables.sys.status": "enabled", "iptables.sys.tcpmss.status": "enabled",
        "iptables.sys.upnpd.status": "disabled", "iptables.sys.upnpd.devname": "",
        "ppp.status": "enabled", "ppp.1.devname": wan, "ppp.1.name": pppoe_usuario, "ppp.1.password": pppoe_clave,
        "ppp.1.pppoe_service": "", "ppp.1.require.mppe128": "disabled", "ppp.1.mtu": "1492", "ppp.1.mru": "1492",
        "ppp.1.status": "enabled", "ppp.1.fallback": "192.168.10.1", "ppp.1.fallback_netmask": "255.255.255.0",
        "ppp.1.ipv6.status": "disabled", "ppp.1.mru_negotiation": "enabled",
        # Por la VLAN de administración se responde a la VPN del sistema; lo demás sale por el PPPoE.
        "route.status": "enabled", "route.1.ip": "", "route.1.netmask": "", "route.1.gateway": "",
        "route.1.comment": "", "route.1.status": "disabled",
        "route6.status": "enabled", "route6.1.ip": "", "route6.1.netmask": "", "route6.1.gateway": "",
        "route6.1.comment": "", "route6.1.status": "disabled",
        "dnsmasq.status": "enabled", "dnsmasq.1.devname": "eth0", "dnsmasq.1.status": "enabled",
        "dhcpd.status": "enabled", "dhcpd.1.status": "enabled", "dhcpd.1.dns.1.server": "", "dhcpd.1.dns.1.status": "disabled",
        "dhcpd.1.dns.2.server": "", "dhcpd.1.dns.2.status": "disabled", "dhcpd.1.devname": "eth0",
        "dhcpd.1.start": mercusys, "dhcpd.1.end": mercusys, "dhcpd.1.netmask": str(lan.netmask),
        "dhcpd.1.lease_time": "600", "dhcpd.1.dnsproxy": "enabled",
        "ebtables.status": "disabled", "ebtables.sys.status": "disabled", "ebtables.sys.vlan.status": "disabled", "upnpd.status": "disabled", "upnpd.devname": "",
        "gui.network.advanced.status": "enabled",
        "unms.status": "disabled",
        "resolv.host.1.name": nombre, "resolv.host.1.status": "enabled",
    })
    if con_admin and ruta_vpn and gateway:
        c.update({"route.1.ip": str(ruta_vpn.network_address), "route.1.netmask": str(ruta_vpn.netmask),
                  "route.1.gateway": gateway, "route.1.comment": "VPN FdezPay", "route.1.status": "enabled"})
    if usuario and clave_hash:
        c.update({"users.status": "enabled", "users.1.status": "enabled", "users.1.name": usuario, "users.1.password": clave_hash})
    return c


async def plantilla_de_instalacion(db: AsyncSession) -> Dict[str, Any]:
    """Para probar la antena antes de dar de alta al cliente (como la ONU de fibra):
    modo router con el PPPoE de instalación (la cuenta FdezNet de cada MikroTik),
    sin IP de administración y con la clave de fábrica, para que la app vuelva a
    entrar y le ponga los datos del cliente al activarlo."""
    config = await config_antenas(db)
    usuario = config.get("antena_pppoe_instalacion_usuario") or ""
    clave = config.get("antena_pppoe_instalacion_clave") or ""
    if not usuario or not clave:
        raise ValueError("Falta el PPPoE de instalación de antenas en la configuración.")
    vlan = config.get("antena_vlan") or "100"
    vlan_cliente = config.get("antena_vlan_cliente") or "10"
    lan_ip = str(ipaddress.ip_address(config.get("antena_lan_ip") or "192.168.20.1"))
    pais = config.get("antena_pais") or "511"
    potencia = config.get("antena_potencia") or "5"
    if not pais.isdigit() or not potencia.isdigit():
        raise ValueError("El país o la potencia de las antenas no son válidos.")
    # Usuario de la empresa para todas las antenas de clientes (el dueño y los
    # técnicos entran igual que a las de Flores Magón): la app lo pone en cuanto
    # entra a una antena de fábrica y con él vuelve a entrar en cada paso.
    acceso_usuario = config.get("antena_usuario") or ""
    acceso_clave = config.get("antena_clave") or ""
    acceso_hash = config.get("antena_clave_hash") or ""
    return {
        "pppoe_usuario": usuario,
        "lan_ip": lan_ip,
        "pais": pais,
        "potencia": int(potencia),
        "acceso": {"usuario": acceso_usuario, "clave": acceso_clave, "clave_hash": acceso_hash}
        if acceso_usuario and acceso_clave and acceso_hash else None,
        "borrar": list(PREFIJOS_DE_RED),
        "claves": bloque_router(
            vlan=int(vlan), vlan_cliente=int(vlan_cliente), ip=None, mascara=None, gateway=None, lan_ip=lan_ip,
            pppoe_usuario=usuario, pppoe_clave=clave, ruta_vpn=None, nombre="Instalacion FdezNet",
            usuario=None, clave_hash=None,
        ),
    }


def _mac(texto: Any) -> str:
    return re.sub(r"[^0-9A-F]", "", str(texto or "").upper())


async def sesion_de_instalacion(db: AsyncSession, mac: str) -> Optional[Dict[str, Any]]:
    """¿Hay en algún MikroTik una sesión del PPPoE de instalación con la MAC de esta antena?"""
    import asyncio

    from src.infrastructure.mikrotik_service import MikroTikService
    from src.infrastructure.models import RouterModel

    buscada = _mac(mac)
    if len(buscada) != 12:
        raise ValueError("MAC de la antena inválida.")
    usuario = (await config_antenas(db)).get("antena_pppoe_instalacion_usuario") or ""
    routers = (await db.execute(select(RouterModel))).scalars().all()

    def buscar(router: RouterModel):
        mk = MikroTikService(router.ip_vpn, router.user_api, router.pass_api, router.port_api)
        for activa in mk._request("GET", "/ppp/active", None, True) or []:
            if activa.get("name") == usuario and _mac(activa.get("caller-id")) == buscada:
                return {"router": router.nombre, "ip": activa.get("address"), "uptime": activa.get("uptime")}
        return None

    for router in routers:
        try:
            encontrada = await asyncio.to_thread(buscar, router)
        except Exception:
            continue
        if encontrada:
            return encontrada
    return None


async def plantilla_de_cliente(db: AsyncSession, cliente: ClienteModel) -> Dict[str, Any]:
    """Lo que la app escribe en la antena del cliente (modo router con su PPPoE)."""
    if cliente.tipo_conexion != "radio":
        raise ValueError("El cliente no es de antena.")
    if not cliente.ip_antena:
        raise ValueError("El cliente no tiene IP de antena: revisa el pool de antenas de su MikroTik.")
    if not cliente.user_pppoe or not cliente.pass_pppoe:
        raise ValueError("El cliente no tiene usuario y clave PPPoE.")
    red = await PoolAntenasService(db).pool_de_router(cliente.router_id)
    if not red:
        raise ValueError("El MikroTik del cliente no tiene pool de antenas en Redes.")
    network = ipaddress.ip_network(red.cidr, strict=False)
    if ipaddress.ip_address(cliente.ip_antena) not in network:
        raise ValueError(f"La IP de antena {cliente.ip_antena} no es del pool {red.cidr}.")
    if not red.gateway:
        raise ValueError(f"El pool de antenas {red.nombre} no tiene gateway.")
    config = await config_antenas(db)
    vlan = config.get("antena_vlan") or "100"
    vlan_cliente = config.get("antena_vlan_cliente") or "10"
    for valor in (vlan, vlan_cliente):
        if not valor.isdigit() or not 1 <= int(valor) <= 4094:
            raise ValueError("La VLAN de administración o de clientes de las antenas no es válida.")
    if vlan == vlan_cliente:
        raise ValueError("La VLAN de administración y la de clientes de las antenas deben ser distintas.")
    try:
        lan_ip = str(ipaddress.ip_address(config.get("antena_lan_ip") or "192.168.20.1"))
        ruta_vpn = ipaddress.ip_network(config.get("antena_ruta_vpn") or "10.8.0.0/24", strict=False)
    except ValueError as exc:
        raise ValueError("La LAN o la ruta de la VPN de las antenas no son válidas.") from exc
    nombre = nombre_de_equipo(cliente.nombre)
    usuario = config.get("antena_usuario") or None
    clave_hash = config.get("antena_clave_hash") or None
    return {
        "ip": cliente.ip_antena,
        "mascara": str(network.netmask),
        "gateway": red.gateway,
        "vlan": int(vlan),
        "vlan_cliente": int(vlan_cliente),
        "lan_ip": lan_ip,
        "nombre": nombre,
        "usuario": usuario,
        "usuario_empresa": bool(usuario and clave_hash),
        "pppoe_usuario": cliente.user_pppoe,
        "borrar": list(PREFIJOS_DE_RED),
        "claves": bloque_router(
            vlan=int(vlan), vlan_cliente=int(vlan_cliente), ip=cliente.ip_antena, mascara=str(network.netmask), gateway=red.gateway, lan_ip=lan_ip,
            pppoe_usuario=cliente.user_pppoe, pppoe_clave=cliente.pass_pppoe, ruta_vpn=ruta_vpn,
            nombre=nombre, usuario=usuario, clave_hash=clave_hash,
        ),
    }


async def ap_del_sector(db: AsyncSession, router_id: Optional[int], ssid: str, frecuencia: Any) -> Optional[EquipoRedModel]:
    """El AP registrado con ese SSID; si hay varios, el de frecuencia más cercana (mismo MikroTik primero)."""
    aps = (
        await db.execute(select(EquipoRedModel).where(EquipoRedModel.tipo == "ap", EquipoRedModel.ssid == ssid))
    ).scalars().all()
    if not aps:
        return None
    f = _mhz(frecuencia)

    def distancia(ap: EquipoRedModel):
        fa = _mhz(ap.frecuencia)
        return (ap.router_id != router_id, abs(fa - f) if f is not None and fa is not None else 10_000)

    return sorted(aps, key=distancia)[0]
