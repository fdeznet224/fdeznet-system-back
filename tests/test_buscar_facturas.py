"""Buscar facturas por nombre, contrato o folio en todas las fechas."""

import asyncio
from datetime import date
from types import SimpleNamespace

from src.interfaces.api import finanzas

ADMIN = SimpleNamespace(rol="admin")


class _DB:
    def __init__(self):
        self.consultas = []

    async def execute(self, consulta):
        self.consultas.append(str(consulta.compile(compile_kwargs={"literal_binds": True})))
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))


def _buscar(busqueda):
    db = _DB()
    asyncio.run(finanzas.get_listado_completo(
        start_date=date(2026, 10, 1), end_date=date(2026, 10, 31), tipo_fecha="vencimiento",
        estado="cualquiera", router_id=None, cliente_id=None, busqueda=busqueda, db=db, current_user=ADMIN,
    ))
    return db.consultas[0]


def test_la_busqueda_ignora_el_rango_de_fechas():
    sql = _buscar("perez")
    assert "fecha_vencimiento >=" not in sql
    assert "'%perez%'" in sql


def test_el_contrato_escrito_con_o_encuentra_el_del_cero():
    assert "'BD0F'" in _buscar("bdof")


def test_un_numero_busca_tambien_el_folio_de_la_factura():
    sql = _buscar("#1138")
    assert "facturas.id = 1138" in sql


def test_sin_busqueda_se_filtra_por_el_mes():
    assert "fecha_vencimiento >=" in _buscar(None)
