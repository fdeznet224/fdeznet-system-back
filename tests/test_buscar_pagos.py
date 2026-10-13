"""Buscar pagos en el corte de cobranza por nombre, contrato, número o referencia."""

import asyncio
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.interfaces.api import finanzas

ADMIN = SimpleNamespace(rol="admin", id=1)


class _DB:
    def __init__(self):
        self.consultas = []

    async def execute(self, consulta):
        self.consultas.append(str(consulta.compile(compile_kwargs={"literal_binds": True})))
        return SimpleNamespace(all=lambda: [])


def _buscar(busqueda, start_date=None, end_date=None):
    db = _DB()
    asyncio.run(finanzas.obtener_reporte_cobranza(
        start_date=start_date, end_date=end_date, busqueda=busqueda, usuario_id=None, router_id=None,
        zona_id=None, db=db, current_user=ADMIN,
    ))
    return db.consultas[0]


def test_la_busqueda_es_en_todas_las_fechas():
    sql = _buscar("beimar", date(2026, 10, 12), date(2026, 10, 12))
    assert "'%beimar%'" in sql and "DATE(pagos.fecha_pago) >=" not in sql.replace("date(", "DATE(")


def test_un_numero_busca_el_pago_y_la_factura():
    sql = _buscar("#1050")
    assert "pagos.id = 1050" in sql and "pagos.factura_id = 1050" in sql


def test_busca_por_referencia_y_contrato():
    sql = _buscar("a3f5")
    assert "lower(pagos.referencia) LIKE '%a3f5%'" in sql
    assert "'A3F5'" in sql


def test_sin_busqueda_se_filtra_por_fechas():
    assert "2026-10-12" in _buscar(None, date(2026, 10, 12), date(2026, 10, 12))


def test_sin_fechas_ni_busqueda_es_un_error():
    with pytest.raises(HTTPException):
        _buscar(None)


class _DBEstadisticas:
    def __init__(self, respuestas):
        self.respuestas = list(respuestas)
        self.consultas = []

    async def execute(self, consulta):
        self.consultas.append(str(consulta.compile(compile_kwargs={"literal_binds": True})))
        filas = self.respuestas.pop(0)
        return SimpleNamespace(all=lambda: filas)


def test_estadisticas_facturado_sin_anuladas_y_cobrado_por_mes():
    db = _DBEstadisticas([
        [SimpleNamespace(mes=9, total=9000), SimpleNamespace(mes=10, total=9500)],
        [SimpleNamespace(mes=10, total=7000)],
    ])
    datos = asyncio.run(finanzas.get_estadisticas(anio=2026, router_id=None, db=db, current_user=ADMIN))
    assert datos == [
        {"mes": 9, "total": 9000.0, "cobrado": 0.0},
        {"mes": 10, "total": 9500.0, "cobrado": 7000.0},
    ]
    assert "'anulada'" in db.consultas[0] and "pagos.estado = 'aplicado'" in db.consultas[1]
