"""Puerto web del router de los clientes por radio (configurable por ISP)."""

import asyncio
from types import SimpleNamespace

import pytest

from src.application.services import acceso_onu_config as config


class _DB:
    def __init__(self, filas=()):
        self.filas = {f.clave: f for f in filas}

    async def execute(self, _consulta):
        filas = list(self.filas.values())
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: filas))

    def add(self, fila):
        self.filas[fila.clave] = fila

    async def commit(self):
        return None


def test_sin_guardar_el_puerto_es_8080():
    assert asyncio.run(config.obtener_acceso_onu(_DB()))["radio"] == {"puerto": "8080"}


def test_se_guarda_el_puerto_y_vacio_es_el_80():
    db = _DB()
    datos = asyncio.run(config.guardar_acceso_onu(db, {"radio": {"puerto": " 8888 "}}))
    assert datos["radio"]["puerto"] == "8888"
    assert asyncio.run(config.guardar_acceso_onu(db, {"radio": {"puerto": ""}}))["radio"]["puerto"] == ""


@pytest.mark.parametrize("puerto", ["abc", "0", "70000"])
def test_un_puerto_invalido_se_rechaza(puerto):
    with pytest.raises(ValueError):
        asyncio.run(config.guardar_acceso_onu(_DB(), {"radio": {"puerto": puerto}}))


def test_puertos_de_onu_por_marca_y_de_la_antena():
    datos = asyncio.run(config.obtener_acceso_onu(_DB()))
    assert datos["huawei"]["puerto"] == "80"
    assert datos["zte"]["puerto"] == "8090"
    assert datos["antena"] == {"puerto": "443"}
    db = _DB()
    datos = asyncio.run(config.guardar_acceso_onu(db, {"zte": {"puerto": "8091"}, "antena": {"puerto": "80"}}))
    assert datos["zte"]["puerto"] == "8091"
    assert datos["antena"]["puerto"] == "80"
    # Vacío regresa al de fábrica de la marca.
    assert asyncio.run(config.guardar_acceso_onu(db, {"zte": {"puerto": ""}}))["zte"]["puerto"] == "8090"
