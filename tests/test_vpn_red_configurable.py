"""Un ISP mudado desde su VPS conserva la red y los nombres de su VPN en la nube."""
import importlib

from src.application.services import vpn_service


def test_por_defecto_la_nube_usa_su_propia_red(monkeypatch):
    for variable in ("VPN_SUBNET_BASE", "VPN_INTERFAZ_MIKROTIK", "VPN_MARCA_VIGILANTE"):
        monkeypatch.delenv(variable, raising=False)
    modulo = importlib.reload(vpn_service)
    assert (modulo.RED_VPN, modulo.INTERFAZ_MIKROTIK, modulo.MARCA_VIGILANTE) == (
        "10.9.0", "wg-wispay", "wispay-vpn-watchdog")


def test_isp_mudado_conserva_la_red_de_su_vps(monkeypatch):
    monkeypatch.setenv("VPN_SUBNET_BASE", "10.8.0.")
    monkeypatch.setenv("VPN_INTERFAZ_MIKROTIK", "wg-fdeznet")
    monkeypatch.setenv("VPN_MARCA_VIGILANTE", "fdeznet-vpn-watchdog")
    modulo = importlib.reload(vpn_service)
    try:
        assert modulo.RED_VPN == "10.8.0"
        script = modulo.construir_vigilante_mikrotik("13.140.231.11", 51822, "10.8.0.1")
        assert 'interface=\\"wg-fdeznet\\"' in script and 'name="fdeznet-vpn-watchdog"' in script
        assert "13.140.231.11:51822" in script
    finally:
        monkeypatch.undo()
        importlib.reload(vpn_service)
