import asyncio
from types import SimpleNamespace

import src.interfaces.api.ftth as ftth


def test_tiempo_encendida_en_palabras():
    assert ftth._texto_encendida("04:46:14") == "4 h 46 min"
    assert ftth._texto_encendida("1 01:05:35") == "1 día 1 h 5 min"
    assert ftth._texto_encendida("00:00:16") == "0 min"
    assert ftth._texto_encendida(None) is None


class _Resultado:
    def __init__(self, cliente):
        self.cliente = cliente

    def scalar_one_or_none(self):
        return self.cliente


class _DB:
    def __init__(self, cliente):
        self.cliente = cliente

    async def execute(self, _consulta):
        return _Resultado(self.cliente)


def _cliente(api_enabled=True, tipo="vsol_api"):
    return SimpleNamespace(
        olt=SimpleNamespace(nombre="Vicente Guerrero", api_enabled=api_enabled, tipo_integracion=tipo),
        onu_asignada=SimpleNamespace(identificador="HWTC05450CB6"),
    )


def test_estado_de_la_onu_por_api(monkeypatch):
    class _Vsol:
        def __init__(self, _db):
            pass

        async def monitorear_objetivo(self, _cliente):
            return {"estado_fisico": "online", "rx_power": "-20.86", "tx_power": "2.1", "alive_time": "04:46:14",
                    "last_deregister_time": "2026/10/05 21:54:01", "last_deregister_reason": "Power Off",
                    "modelo": "HG8145V5V3"}

    monkeypatch.setattr(ftth, "VsolApiService", _Vsol)
    estado = asyncio.run(ftth.estado_onu(5, db=_DB(_cliente()), current_user=None))

    assert estado["online"] is True and estado["rx"] == -20.86 and estado["encendida"] == "4 h 46 min"
    assert estado["ultima_caida"] == "2026/10/05 21:54:01" and estado["causa_ultima_caida"]
    assert estado["puede_reiniciar"] is True


def test_por_snmp_no_se_puede_reiniciar_y_una_olt_caida_no_truena(monkeypatch):
    class _Snmp:
        def __init__(self, _db):
            pass

        async def monitorear_objetivo(self, _cliente):
            raise TimeoutError("sin respuesta")

    monkeypatch.setattr(ftth, "SNMPMonitorService", _Snmp)
    estado = asyncio.run(ftth.estado_onu(5, db=_DB(_cliente(api_enabled=False, tipo="snmp")), current_user=None))
    assert estado["disponible"] is False and "no respondió" in estado["error"]
