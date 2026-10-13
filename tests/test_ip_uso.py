"""IP que el MikroTik usa sin cliente en el sistema y búsqueda del Panel OLT."""

import asyncio
from types import SimpleNamespace

import pytest

from src.application.services import ip_uso_service as modulo
from src.application.services.ip_uso_service import IpUsoService, ips_vistas
from src.application.services.panel_olt_service import coincide


def test_junta_lo_que_ve_el_mikrotik_y_descarta_lo_que_no_es_ip():
    vistas = ips_vistas(
        secrets=[{"name": "juan", "remote-address": "10.10.0.5"}, {"name": "sin", "remote-address": ""}],
        activos=[{"name": "juan", "address": "10.10.0.5"}],
        arp=[{"address": "10.10.0.2", "mac-address": "AA:BB", "comment": "Casa"},
             {"address": "10.10.0.9", "complete": "false"}],
        leases=[{"address": "10.10.0.30", "host-name": "site"}],
        propias=[{"address": "10.10.0.1/24", "interface": "bridge"}],
    )
    assert vistas == {
        "10.10.0.1": "Dirección del MikroTik (bridge)",
        "10.10.0.5": "Sesión PPPoE activa: juan",
        "10.10.0.30": "DHCP: site",
        "10.10.0.2": "Equipo en ARP: Casa",
    }


def test_el_pppoe_del_propio_servicio_no_choca_consigo():
    vistas = ips_vistas([{"name": "juan", "remote-address": "10.10.0.5"}], [], [], [], [], ignorar_usuario="juan")
    assert vistas == {}


class _DB:
    def __init__(self, router):
        self.router = router

    async def get(self, _modelo, _llave):
        return self.router


def _red(**kw):
    datos = dict(id=1, nombre="Clientes VG", cidr="10.10.0.0/24", gateway="10.10.0.1", router_id=3)
    datos.update(kw)
    return SimpleNamespace(**datos)


def test_las_ip_fuera_del_sistema_no_salen_como_libres(monkeypatch):
    vistas = {"10.10.0.1": "MikroTik", "10.10.0.2": "Equipo en ARP: Casa", "10.10.0.3": "Usuario PPPoE: ana",
              "192.168.88.1": "Otra red"}
    monkeypatch.setattr(modulo, "_leer_mikrotik", lambda router, usuario=None: vistas)

    async def ocupadas(self, *_args):
        return {"10.10.0.3"}

    monkeypatch.setattr(modulo.IPAMService, "_ips_ocupadas", ocupadas)
    uso = asyncio.run(IpUsoService(_DB(SimpleNamespace(nombre="VG"))).uso_de_red(_red()))

    assert uso["mikrotik_verificado"] is True
    assert uso["disponibles"][:2] == ["10.10.0.4", "10.10.0.5"]
    # La de ana es de un cliente; el gateway no es conflicto.
    assert uso["fuera_del_sistema"] == [{"ip": "10.10.0.2", "origen": "Equipo en ARP: Casa"}]


def test_sin_mikrotik_avisa_y_no_inventa(monkeypatch):
    def falla(router, usuario=None):
        raise RuntimeError("timeout")

    monkeypatch.setattr(modulo, "_leer_mikrotik", falla)
    en_mk, error = asyncio.run(IpUsoService(_DB(SimpleNamespace(nombre="VG"))).ips_en_mikrotik(_red()))
    assert en_mk == {} and "VG" in error


@pytest.mark.parametrize("texto", ["HWTC1A2B3C4D", "hwtc1a2b", "José Pérez", "jose perez", "0/3:12", "10.10.0.8", "FN-0042"])
def test_busca_por_sn_onu_y_datos_del_cliente(texto):
    auth = {"info": "HWTC1A2B3C4D", "description": "FN-0042", "onu_id": "GPON0/3:12"}
    assert coincide(texto, auth, "José Pérez FN-0042 jperez 10.10.0.8")


def test_no_trae_lo_que_no_coincide():
    assert not coincide("maria", {"info": "HWTC1A2B3C4D", "description": "x"}, "José Pérez")
