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


def _servicio(id, serial, nombre, onu_ficha=None):
    return SimpleNamespace(id=id, cliente_id=100 + id, onu_id=200 + id if serial else None, estado="activo",
                           onu=SimpleNamespace(id=200 + id, identificador=serial) if serial else None,
                           cliente=SimpleNamespace(nombre=nombre, cedula=f"C{id}", onu_asignada=onu_ficha))


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

    reporte = asyncio.run(SenalOpticaService(db, lector=lector).tomar_lecturas(evaluar_alertas=True))

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
    assert "…y 2 más (filtro «Potencia alta» en Clientes)." in texto
    assert mensaje_alerta({"alertas": []}) is None


def test_cada_hora_solo_guarda_y_no_evalua_avisos():
    db = _DB([_servicio(1, "HWTC05450CB6", "Ana")], [SimpleNamespace(id=1, nombre="Villa")], {1: Decimal("-21.00")})

    async def lector(_olt):
        return [{"identificador": "HWTC05450CB6", "rx": "-28.10"}]

    reporte = asyncio.run(SenalOpticaService(db, lector=lector).tomar_lecturas())
    assert reporte["leidas"] == 1 and reporte["alertas"] == []


def test_usa_la_onu_de_la_ficha_si_el_servicio_no_la_tiene_o_quedo_vieja():
    # Victor: la ONU se puso editando la ficha y el servicio se quedó sin ella.
    # Rosa: se cambió la ONU en la ficha y el servicio sigue con la anterior.
    servicios = [
        _servicio(1, None, "Victor", onu_ficha=SimpleNamespace(id=301, identificador="ZTEG25520C10")),
        _servicio(2, "HWTCVIEJA001", "Rosa", onu_ficha=SimpleNamespace(id=302, identificador="E0:0C:E5:27:75:E2")),
    ]
    db = _DB(servicios, [SimpleNamespace(id=1, nombre="Villa")], {})

    async def lector(_olt):
        return [{"identificador": "ZTEG25520C10", "rx": "-22.40"},
                {"identificador": "e00ce52775e2", "rx": "-19.10"},
                {"identificador": "HWTCVIEJA001", "rx": "-30.00"}]

    reporte = asyncio.run(SenalOpticaService(db, lector=lector).tomar_lecturas())
    assert reporte["leidas"] == 2
    assert [(l.servicio_id, l.onu_id, l.potencia_rx_dbm) for l in db.agregados] == [
        (1, 301, Decimal("-22.40")), (2, 302, Decimal("-19.10"))]


class _DBPotencias:
    def __init__(self, filas):
        self.filas = filas

    async def execute(self, _consulta):
        filas = self.filas

        class _R:
            def all(self):
                return filas

        return _R()


def test_la_potencia_del_cliente_es_la_peor_de_sus_domicilios():
    from datetime import datetime

    fecha = datetime(2026, 10, 6, 10, 5)
    db = _DBPotencias([(5, Decimal("-21.00"), fecha), (5, Decimal("-28.40"), fecha), (6, Decimal("-19.5"), fecha)])
    potencias = asyncio.run(SenalOpticaService(db).potencias_por_cliente())
    assert potencias[5] == {"rx": -28.4, "nivel": "alta", "fecha": "2026-10-06T10:05:00"}
    assert potencias[6]["nivel"] == "normal"
