"""Cada zona define su MikroTik, su OLT y su plantilla de cobro."""

import asyncio
from types import SimpleNamespace

import pytest

from src.application.services.zone_service import ZoneService
from src.domain.schemas import ZonaCreate

OLTS = {7: SimpleNamespace(id=7, router_id=2), 2: SimpleNamespace(id=2, router_id=3)}
ROUTERS = {2: SimpleNamespace(id=2), 3: SimpleNamespace(id=3)}
PLANTILLAS = {1: SimpleNamespace(id=1), 2: SimpleNamespace(id=2)}


class _DB:
    def __init__(self, zona=None):
        self.zona = zona
        self.agregados = []

    async def get(self, modelo, id_):
        return {
            "OLTModel": OLTS, "RouterModel": ROUTERS, "PlantillaFacturacionModel": PLANTILLAS,
            "ZonaModel": {1: self.zona} if self.zona else {},
        }[modelo.__name__].get(id_)

    def add(self, objeto):
        self.agregados.append(objeto)

    async def commit(self):
        return None

    async def refresh(self, objeto):
        return None


def test_la_olt_completa_el_mikrotik_de_la_zona():
    db = _DB()
    zona = asyncio.run(ZoneService(db).crear_zona(ZonaCreate(nombre="Vicente Guerrero", olt_id=7, plantilla_id=1)))
    assert (zona.router_id, zona.olt_id, zona.plantilla_id) == (2, 7, 1)


def test_zona_por_radioenlace_sin_olt():
    zona = asyncio.run(ZoneService(_DB()).crear_zona(ZonaCreate(nombre="Sagrado Corazón", router_id=2, plantilla_id=2)))
    assert (zona.router_id, zona.olt_id) == (2, None)


def test_no_acepta_una_olt_de_otro_mikrotik():
    with pytest.raises(ValueError, match="otro MikroTik"):
        asyncio.run(ZoneService(_DB()).crear_zona(ZonaCreate(nombre="La Merced", router_id=2, olt_id=2)))


def test_editar_actualiza_la_infraestructura():
    zona = SimpleNamespace(id=1, nombre="Paraiso", router_id=None, olt_id=None, plantilla_id=None)
    asyncio.run(ZoneService(_DB(zona)).editar_zona(1, ZonaCreate(nombre="Paraíso", olt_id=2, plantilla_id=2)))
    assert (zona.nombre, zona.router_id, zona.olt_id, zona.plantilla_id) == ("Paraíso", 3, 2, 2)


def test_editar_una_zona_que_no_existe():
    with pytest.raises(LookupError):
        asyncio.run(ZoneService(_DB()).editar_zona(1, ZonaCreate(nombre="X1")))
