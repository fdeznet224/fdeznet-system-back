"""IP fija/DHCP: estado y tráfico por IP; editar subredes de un túnel."""
import asyncio
from types import SimpleNamespace

import pytest

from src.application.services.network_service import NetworkService
from src.application.services.vpn_service import VPNService
from src.infrastructure.mikrotik_service import MikroTikService


class MkFalso(MikroTikService):
    def __init__(self, respuestas):
        self.respuestas = respuestas

    def _request(self, method, endpoint, payload=None, raise_on_error=False):
        for clave, valor in self.respuestas.items():
            if endpoint.startswith(clave):
                return valor
        return []


def test_ip_fija_en_linea_por_ping():
    mk = MkFalso({"/ping": [{"time": "3ms"}, {"time": "2ms"}, {"time": "2ms"}], "/ip/arp": []})
    estado = mk.estado_por_ip("10.10.5.20")
    assert estado["online"] is True
    assert estado["datos"]["ip_actual"] == "10.10.5.20"


def test_ip_fija_sin_ping_pero_en_arp_cuenta_como_en_linea():
    mk = MkFalso({"/ping": [{"status": "timeout"}], "/ip/arp": [{"mac-address": "AA:BB:CC:00:11:22", "status": "reachable"}]})
    estado = mk.estado_por_ip("10.10.5.20")
    assert estado["online"] is True
    assert "ARP" in estado["datos"]["info"]


def test_arp_estatica_o_fallida_no_cuenta():
    for status in ("permanent", "failed", "stale"):
        mk = MkFalso({"/ping": [], "/ip/arp": [{"mac-address": "AA:BB:CC:00:11:22", "status": status}]})
        assert mk.estado_por_ip("10.10.5.20")["online"] is False


def test_trafico_por_ip_lee_la_cola_simple_del_cliente():
    mk = MkFalso({"/queue/simple": [
        {"target": "10.10.5.21/32", "rate": "1/1"},
        {"target": "10.10.5.20/32", "rate": "500000/8000000"},
    ]})
    # up_bps = bajada del cliente (mismo sentido que PPPoE)
    assert mk.trafico_por_ip("10.10.5.20") == {"up_bps": 8000000, "down_bps": 500000}


def test_estado_en_vivo_usa_el_modo_del_router():
    servicio = SimpleNamespace(user_pppoe=None, ip_asignada="10.10.5.20", mac_address=None)
    mk = MkFalso({"/ping": [{"time": "1ms"}], "/ip/arp": []})
    estado = NetworkService._estado_en_vivo(mk, SimpleNamespace(tipo_seguridad="estatica"), servicio)
    assert estado["online"] is True and estado["metodo"] == "IP fija"
    # Router PPPoE sin usuario: no se inventa nada
    estado = NetworkService._estado_en_vivo(mk, SimpleNamespace(tipo_seguridad="pppoe"), servicio)
    assert estado == {"online": False, "metodo": "PPPoE", "datos": {}}


class DbFalsa:
    async def commit(self):
        pass

    async def refresh(self, _):
        pass


def _vpn(salida_allowed: str):
    vpn = VPNService.__new__(VPNService)
    vpn.db = DbFalsa()
    vpn.WG_BIN, vpn.WG_QUICK_BIN, vpn.WG_INTERFACE = "wg", "wg-quick", "wg0"
    vpn.SERVER_PUBKEY = "server"
    vpn.comandos = []

    def ejecutar(comando, input_str=None):
        vpn.comandos.append(comando)
        return salida_allowed if "allowed-ips" in comando and "show" in comando else ""

    vpn._ejecutar_comando = ejecutar
    return vpn


def test_editar_subredes_agrega_red_sin_chocar_con_las_propias():
    vpn = _vpn("PROPIO\t10.9.0.2/32 192.168.1.0/24\nOTRO\t10.9.0.3/32 172.16.0.0/24")
    tunel = SimpleNamespace(public_key="PROPIO", ip_asignada="10.9.0.2", script_mikrotik="/interface wireguard add", subredes_remotas="192.168.1.0/24")
    asyncio.run(vpn.editar_subredes(tunel, "192.168.1.0/24\n10.10.5.0/24"))
    assert tunel.subredes_remotas == "192.168.1.0/24, 10.10.5.0/24"
    assert any("10.9.0.2/32,192.168.1.0/24,10.10.5.0/24" in c for c in vpn.comandos)


def test_editar_subredes_rechaza_red_de_otro_peer():
    vpn = _vpn("PROPIO\t10.9.0.2/32\nOTRO\t10.9.0.3/32 192.168.1.0/24 172.16.0.0/24")
    tunel = SimpleNamespace(public_key="PROPIO", ip_asignada="10.9.0.2", script_mikrotik="x", subredes_remotas=None)
    with pytest.raises(ValueError, match="172.16.0.0/24"):
        asyncio.run(vpn.editar_subredes(tunel, "172.16.0.0/24"))


def test_app_movil_no_se_edita():
    vpn = _vpn("")
    tunel = SimpleNamespace(public_key="P", ip_asignada="10.9.0.9", script_mikrotik="[Interface]\nPrivateKey=x", subredes_remotas=None)
    with pytest.raises(ValueError):
        asyncio.run(vpn.editar_subredes(tunel, "10.10.5.0/24"))
