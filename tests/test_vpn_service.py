import os
import pytest

from src.application.services.vpn_service import VPNService


def test_normaliza_varias_subredes_remotas():
    assert VPNService.normalizar_subredes(
        "192.168.21.8/24, 10.10.9.0/24\n192.168.21.0/24"
    ) == ["192.168.21.0/24", "10.10.9.0/24"]


@pytest.mark.parametrize(
    "value",
    ["sin-cidr", "0.0.0.0/0", "10.9.0.0/25", "2001:db8::/64"],
)
def test_rechaza_subredes_no_seguras_o_invalidas(value):
    with pytest.raises(ValueError):
        VPNService.normalizar_subredes(value)


from src.application.services.vpn_service import (
    MARCA_VIGILANTE,
    asegurar_vigilante,
    construir_vigilante_mikrotik,
    parsear_handshakes,
)

SCRIPT_ANTERIOR = """# === FDEZNET VPN SCRIPT - TORRE ===
/interface wireguard add listen-port=13231 name=wg-fdeznet private-key="x"
/interface wireguard peers add allowed-address=10.8.0.0/24 \\
    endpoint-address=203.0.113.10 endpoint-port=51820 \\
    interface=wg-fdeznet public-key="y" persistent-keepalive=25s
/ip address add address=10.8.0.4/24 interface=wg-fdeznet"""


def test_vigilante_reemplaza_el_anterior_y_limpia_la_conexion_udp():
    script = construir_vigilante_mikrotik("203.0.113.10", 51820, "10.8.0.1")

    assert f'/system scheduler remove [find name="{MARCA_VIGILANTE}"]' in script
    assert f'/system script remove [find name="{MARCA_VIGILANTE}"]' in script
    assert "[/ping 10.8.0.1 count=3 interval=1s] = 0" in script
    assert 'dst-address=\\"203.0.113.10:51820\\"' in script
    assert "interval=2m start-time=startup" in script


def test_scripts_anteriores_reciben_el_vigilante_una_sola_vez():
    actualizado = asegurar_vigilante(SCRIPT_ANTERIOR)

    assert actualizado.startswith(SCRIPT_ANTERIOR)
    assert 'dst-address=\\"203.0.113.10:51820\\"' in actualizado
    assert asegurar_vigilante(actualizado) == actualizado


def test_config_de_tecnico_no_recibe_vigilante():
    conf = "[Interface]\nPrivateKey = x\n[Peer]\nEndpoint = 203.0.113.10:51820"
    assert asegurar_vigilante(conf) == conf
    assert asegurar_vigilante(None) is None


def test_parsea_handshakes_por_ip_del_peer():
    dump = (
        "priv\tpub\t51820\toff\n"
        "k1\t(none)\t198.51.100.1:13231\t10.8.0.2/32,10.10.9.0/24\t1758600000\t10\t20\toff\n"
        "k2\t(none)\t(none)\t10.8.0.5/32\t0\t0\t0\toff\n"
    )
    assert parsear_handshakes(dump) == {"10.8.0.2": 1758600000, "10.8.0.5": 0}


def test_comandos_wireguard_sin_sudo_en_contenedor(monkeypatch):
    from src.application.services.vpn_service import con_privilegios

    monkeypatch.setenv("SUDO_BIN", "")
    assert con_privilegios(["/usr/bin/wg", "show"]) == ["/usr/bin/wg", "show"]

    monkeypatch.setenv("SUDO_BIN", "/usr/bin/sudo")
    assert con_privilegios(["/usr/bin/wg", "show"]) == ["/usr/bin/sudo", "/usr/bin/wg", "show"]


def test_nube_convive_con_la_vpn_de_otro_sistema_en_el_mikrotik():
    """El script de la nube no debe pisar wg-fdeznet, su red 10.8.0.0/24 ni su vigilante."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from src.application.services import vpn_service

    os.environ.update(VPN_SERVER_IP="203.0.113.10", VPN_SERVER_PUBKEY="srv=", WG_PORT="51821")
    try:
        servicio = vpn_service.VPNService(MagicMock(commit=AsyncMock(), refresh=AsyncMock()))
    finally:
        for clave in ("VPN_SERVER_IP", "VPN_SERVER_PUBKEY", "WG_PORT"):
            os.environ.pop(clave)
    servicio._ejecutar_comando = lambda *a, **k: "llave"
    servicio.obtener_siguiente_ip = AsyncMock(return_value="10.9.0.2")
    tunel = asyncio.run(servicio.crear_tunel("Torre"))
    script = tunel.script_mikrotik

    assert "name=wg-wispay" in script and "listen-port=13232" in script
    assert "allowed-address=10.9.0.0/24" in script and "address=10.9.0.2/24 interface=wg-wispay" in script
    assert "[/ping 10.9.0.1 " in script and 'name="wispay-vpn-watchdog"' in script
    assert "wg-fdeznet" not in script and "fdeznet-vpn-watchdog" not in script and "10.8.0" not in script
    assert "/ip dns set" not in script and "/system note" not in script
