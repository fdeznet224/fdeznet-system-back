"""Antena del cliente por radioenlace: ping desde su MikroTik."""

import asyncio
from types import SimpleNamespace

import pytest

from src.application.services import antena_service as modulo
from src.application.services.antena_service import AntenaService, milisegundos, resumir_ping


@pytest.mark.parametrize("texto, ms", [("2ms613us", 2.6), ("14ms", 14.0), ("500us", 0.5), ("1s2ms", 1002.0), ("", None), (None, None)])
def test_lee_los_tiempos_de_routeros(texto, ms):
    assert milisegundos(texto) == ms


def test_resume_latencia_y_perdida():
    respuestas = [{"time": "2ms"}, {"time": "4ms"}, {"status": "timeout"}, {"status": "timeout"}]
    assert resumir_ping(respuestas, 4) == {"responde": True, "latencia_ms": 3.0, "perdida": 50}
    assert resumir_ping([{"status": "timeout"}] * 4, 4) == {"responde": False, "latencia_ms": None, "perdida": 100}
    assert resumir_ping(None, 4)["responde"] is False


class _DB:
    def __init__(self, cliente, router=None):
        self.cliente, self.router = cliente, router
        self.commits = 0

    async def get(self, modelo, _llave):
        return self.router if modelo.__name__ == "RouterModel" else self.cliente

    async def commit(self):
        self.commits += 1


def _cliente(**kw):
    datos = dict(id=291, nombre="Concepcion", tipo_conexion="radio", ip_antena="192.168.1.20", router_id=2,
                 antena_responde=None, antena_latencia_ms=None, antena_perdida=None, antena_revisada_en=None)
    datos.update(kw)
    return SimpleNamespace(**datos)


def test_verifica_la_antena_y_guarda_el_resultado(monkeypatch):
    pedidos = []

    class _MK:
        def __init__(self, *a):
            pass

        def _request(self, metodo, ruta, datos, estricto):
            pedidos.append((metodo, ruta, datos))
            return [{"time": "3ms"}] * 4

    monkeypatch.setattr(modulo, "MikroTikService", _MK)
    db = _DB(_cliente(), SimpleNamespace(ip_vpn="1", user_api="u", pass_api="p", port_api=80))
    estado = asyncio.run(AntenaService(db).verificar(291))
    assert pedidos == [("POST", "/ping", {"address": "192.168.1.20", "count": "4"})]
    assert (estado["responde"], estado["latencia_ms"], estado["perdida"]) == (True, 3.0, 0)
    assert estado["fecha"] and db.commits == 1


@pytest.mark.parametrize("cliente, error", [
    (_cliente(tipo_conexion="fibra"), "fibra"),
    (_cliente(ip_antena=None), "IP de la antena"),
])
def test_no_revisa_sin_antena(cliente, error):
    with pytest.raises(ValueError, match=error):
        asyncio.run(AntenaService(_DB(cliente)).verificar(291))


def test_la_ip_de_la_antena_se_valida_y_vacia_es_ninguna():
    from src.domain.schemas import ClienteUpdate

    assert ClienteUpdate(nombre="X", ip_antena=" 192.168.1.20 ").ip_antena == "192.168.1.20"
    assert ClienteUpdate(nombre="X", ip_antena="").ip_antena is None
    assert ClienteUpdate(nombre="X").tipo_conexion is None
    with pytest.raises(ValueError):
        ClienteUpdate(nombre="X", ip_antena="192.168.1")
    with pytest.raises(ValueError):
        ClienteUpdate(nombre="X", tipo_conexion="satelite")
