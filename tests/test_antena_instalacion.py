import ipaddress

from src.application.services.antena_instalacion_service import (
    PREFIJOS_DE_RED,
    bloque_router,
    nombre_de_equipo,
)


def _bloque(**cambios):
    base = dict(vlan=100, ip="13.13.13.28", mascara="255.255.255.0", gateway="13.13.13.1", lan_ip="192.168.20.1",
                pppoe_usuario="Arisel_Fernandez", pppoe_clave="s3creta", ruta_vpn=ipaddress.ip_network("10.8.0.0/24"),
                nombre="Arisel Fernández", usuario="FdezNet", clave_hash="$1$abc$def")
    base.update(cambios)
    return bloque_router(**base)


def test_modo_router_con_pppoe_por_el_radio_y_administracion_en_la_vlan():
    c = _bloque()
    assert c["netmode"] == "router"
    # PPPoE etiquetado en la VLAN de clientes (10) sobre el radio, como espera el MikroTik.
    assert (c["netconf.1.devname"], c["netconf.1.role"]) == ("ath0.10", "wan")
    assert (c["vlan.2.devname"], c["vlan.2.id"]) == ("ath0", "10")
    assert (c["netconf.4.devname"], c["netconf.4.role"], c["netconf.4.status"]) == ("ath0", "", "enabled")
    assert c["dhcpc.1.devname"] == "ath0.10"
    assert (c["netconf.2.devname"], c["netconf.2.role"], c["netconf.2.ip"]) == ("eth0", "lan", "192.168.20.1")
    assert (c["netconf.3.devname"], c["netconf.3.role"], c["netconf.3.ip"]) == ("ath0.100", "mlan", "13.13.13.28")
    assert (c["vlan.1.devname"], c["vlan.1.id"]) == ("ath0", "100")
    assert (c["ppp.1.devname"], c["ppp.1.name"], c["ppp.1.password"]) == ("ath0.10", "Arisel_Fernandez", "s3creta")
    # El Mercusys recibe siempre la .2 y la DMZ le manda el acceso remoto.
    assert c["dhcpd.1.start"] == c["dhcpd.1.end"] == c["iptables.sys.dmz.1.host"] == "192.168.20.2"
    # La administración se bloquea solo por el PPPoE, no por la VLAN.
    assert c["iptables.sys.mgmt.1.devname"] == "ppp+"
    # Regreso a la VPN del sistema por el gateway del pool.
    assert (c["route.1.ip"], c["route.1.netmask"], c["route.1.gateway"]) == ("10.8.0.0", "255.255.255.0", "13.13.13.1")
    assert c["unms.status"] == "disabled"
    assert (c["resolv.host.1.name"], c["users.1.name"], c["users.1.password"]) == ("Arisel Fernández", "FdezNet", "$1$abc$def")
    # Todo lo de red cae dentro de lo que se borra antes de escribir.
    red = [k for k in c if not k.startswith(("resolv.", "users.", "gui."))]
    assert all(k.startswith(PREFIJOS_DE_RED) for k in red)


def test_sin_usuario_de_empresa_no_toca_los_usuarios():
    c = _bloque(usuario=None, clave_hash=None)
    assert not any(k.startswith("users.") for k in c)


def test_nombre_de_equipo_sin_caracteres_que_rompen_el_cfg():
    assert nombre_de_equipo("Ana\nLópez=Pérez") == "Ana López Pérez"
    assert len(nombre_de_equipo("x" * 100)) == 63
    assert nombre_de_equipo("  ") == "Cliente"
