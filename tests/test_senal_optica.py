import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.application.services.senal_optica_service import (
    SenalOpticaService,
    a_dbm,
    evaluar,
    mensaje_alerta,
)
from src.infrastructure.models import LecturaOpticaModel


@pytest.mark.parametrize("valor, esperado", [
    ("-21.35", Decimal("-21.35")), ("-21.35 dBm", Decimal("-21.35")), (-19.5, Decimal("-19.50")),
    ("LOS / Sin señal", None), ("0.00", None), ("N/A", None), (None, None), ("-60", None),
])
def test_lee_la_potencia_y_descarta_sin_senal(valor, esperado):
    assert a_dbm(valor) == esperado


@pytest.mark.parametrize("actual, anterior, motivo", [
    ("-27.5", "-24.0", "debil"),     # cruzó el umbral
    ("-27.5", None, "debil"),        # primera lectura ya débil
    ("-28.0", "-27.6", None),        # ya estaba débil: no se repite el aviso cada noche
    ("-24.5", "-21.0", "caida"),     # cayó 3.5 dB aunque sigue arriba del umbral
    ("-22.0", "-21.0", None),        # variación normal
])
def test_avisa_solo_lo_que_empeoro(actual, anterior, motivo):
    assert evaluar(Decimal(actual), Decimal(anterior) if anterior else None) == motivo


class _Resultado:
    def __init__(self, valor):
        self.valor = valor

    def scalars(self):
        return self

    def all(self):
        return self.valor

    def scalar_one_or_none(self):
        return self.valor


class _DB:
    def __init__(self, servicios, olts, anteriores):
        self.servicios = servicios
        self.olts = olts
        self.anteriores = anteriores  # servicio_id -> Decimal
        self.agregados = []
        self.commits = 0

    async def execute(self, consulta):
        texto = str(consulta)
        if "FROM olts" in texto:
            return _Resultado(self.olts)
        if "FROM lecturas_opticas" in texto:
            servicio_id = consulta.compile().params.get("servicio_id_1")
            return _Resultado(self.anteriores.get(servicio_id))
        return _Resultado(self.servicios)

    def add(self, objeto):
        self.agregados.append(objeto)

    async def commit(self):
        self.commits += 1


def _servicio(id, serial, nombre):
    return SimpleNamespace(id=id, cliente_id=100 + id, onu_id=200 + id, estado="activo",
                           onu=SimpleNamespace(identificador=serial),
                           cliente=SimpleNamespace(nombre=nombre, cedula=f"C{id}"))


def test_guarda_lecturas_cruza_por_serial_y_sigue_si_una_olt_falla():
    servicios = [_servicio(1, "HWTC05450CB6", "Ana"), _servicio(2, "A0:B1:C2:D3:E4:F5", "Beto")]
    olts = [SimpleNamespace(id=1, nombre="Villa"), SimpleNamespace(id=2, nombre="Paraíso"),
            SimpleNamespace(id=3, nombre="Caída")]
    db = _DB(servicios, olts, {1: Decimal("-21.00"), 2: Decimal("-20.00")})

    async def lector(olt):
        if olt.id == 1:
            return [{"identificador": "hwtc05450cb6", "rx": "-25.10", "tx": "2.1"},
                    {"identificador": "DESCONOCIDA1", "rx": "-20"}]
        if olt.id == 2:
            return [{"identificador": "a0b1c2d3e4f5", "rx": "LOS", "tx": None}]
        raise TimeoutError("sin respuesta")

    reporte = asyncio.run(SenalOpticaService(db, lector=lector).tomar_lecturas())

    assert reporte["leidas"] == 1 and reporte["sin_senal"] == 1
    assert reporte["olts_con_error"] == ["Caída"]
    lectura = db.agregados[0]
    assert isinstance(lectura, LecturaOpticaModel)
    assert (lectura.servicio_id, lectura.potencia_rx_dbm, lectura.origen) == (1, Decimal("-25.10"), "automatica")
    assert reporte["alertas"][0]["motivo"] == "caida" and reporte["alertas"][0]["cliente"] == "Ana"


def test_el_aviso_resume_y_corta_la_lista():
    alertas = [{"cliente": f"Cliente {i}", "contrato": None, "olt": "Villa", "rx": -28.0, "anterior": -24.0,
                "motivo": "debil"} for i in range(12)]
    texto = mensaje_alerta({"alertas": alertas}, maximo=10)
    assert texto.startswith("📉 *Señal óptica: 12 clientes empeoraron*")
    assert "…y 2 más en Averías → Señal débil." in texto
    assert mensaje_alerta({"alertas": []}) is None
