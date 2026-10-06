import asyncio
from types import SimpleNamespace

import pytest

from src.application.services.aviso_averia_service import personalizar
from src.application.services.nap_service import NapService
from src.infrastructure.models import CajaNapModel, ClienteModel, ServicioModel


class _Resultado:
    def __init__(self, filas):
        self.filas = filas

    def scalars(self):
        return self

    def all(self):
        return self.filas


class _DB:
    def __init__(self, objetos=None, filas=None):
        self.objetos = objetos or {}
        self.filas = filas or []
        self.consultas = []
        self.commits = 0

    async def get(self, modelo, llave):
        return self.objetos.get((modelo, llave))

    async def execute(self, consulta):
        self.consultas.append(str(consulta))
        return _Resultado(self.filas)

    async def commit(self):
        self.commits += 1


def _servicio(id, telefono, nombre="Ana Lopez", **cambios):
    datos = dict(id=id, cliente_id=100 + id, estado="activo", caja_nap_id=None, puerto_nap=None,
                 latitud=17.1, longitud=-93.2, zona_id=2, alias="Principal", direccion="Calle 1",
                 zona=SimpleNamespace(nombre="Paraíso"),
                 cliente=SimpleNamespace(id=100 + id, nombre=nombre, cedula=f"C{id}", telefono=telefono,
                                         direccion="Calle 1"))
    datos.update(cambios)
    return SimpleNamespace(**datos)


def test_un_cliente_viejo_se_liga_a_su_caja_sin_puerto():
    servicio = _servicio(1, "9611111111")
    cliente = SimpleNamespace(caja_nap_id=None, puerto_nap=None)
    caja = SimpleNamespace(id=8, nombre="P1-SA-1-A", capacidad=8)
    db = _DB({(ServicioModel, 1): servicio, (CajaNapModel, 8): caja, (ClienteModel, 101): cliente})

    resultado = asyncio.run(NapService(db).asignar_nap_servicio(1, 8, usuario_id=1))

    assert (servicio.caja_nap_id, servicio.puerto_nap) == (8, None)
    assert cliente.caja_nap_id == 8
    assert resultado["caja_nombre"] == "P1-SA-1-A" and db.commits == 1


def test_no_acepta_un_puerto_que_la_caja_no_tiene():
    db = _DB({(ServicioModel, 1): _servicio(1, "9611111111"),
              (CajaNapModel, 8): SimpleNamespace(id=8, nombre="P1", capacidad=8)})
    with pytest.raises(ValueError, match="tiene 8 puertos"):
        asyncio.run(NapService(db).asignar_nap_servicio(1, 8, usuario_id=1, puerto_nap=9))


def test_sugiere_cajas_solo_a_quien_tiene_gps(monkeypatch):
    con_gps = _servicio(1, "9611111111")
    sin_gps = _servicio(2, "9622222222", latitud=None, longitud=None)
    db = _DB(filas=[con_gps, sin_gps])
    llamadas = []

    async def candidatas(self, zona_id=None, olt_id=None):
        llamadas.append(zona_id)
        return [{"id": 8, "nombre": "P1", "posicion": (17.1001, -93.2), "puertos_libres": 3}]

    monkeypatch.setattr(NapService, "candidatas", candidatas)
    resultado = asyncio.run(NapService(db).servicios_sin_nap())

    assert [r["tiene_gps"] for r in resultado] == [True, False]
    assert resultado[0]["sugeridas"][0]["id"] == 8 and "posicion" not in resultado[0]["sugeridas"][0]
    assert resultado[1]["sugeridas"] == []
    assert llamadas == [2]  # las cajas de la zona se calculan una sola vez


def test_el_aviso_llega_una_vez_por_telefono_y_omite_sin_telefono():
    db = _DB(filas=[
        _servicio(1, "961 111 1111"),
        _servicio(2, "+52 9611111111"),  # mismo cliente con otro domicilio
        _servicio(3, ""),
        _servicio(4, "9633333333", nombre="Beto"),
    ])
    afectados = asyncio.run(NapService(db).afectados_por_averia(caja_nap_id=8))
    assert [a["cliente_id"] for a in afectados] == [101, 104]
    assert "servicios.caja_nap_id =" in db.consultas[0]


def test_por_puerto_de_olt_toma_todas_sus_cajas():
    db = _DB(filas=[])
    asyncio.run(NapService(db).afectados_por_averia(olt_id=2, puerto_olt=3))
    assert "cajas_nap.olt_id" in db.consultas[0] and "cajas_nap.puerto_olt" in db.consultas[0]


def test_hay_que_elegir_donde_es_la_falla():
    with pytest.raises(ValueError, match="Elige"):
        asyncio.run(NapService(_DB()).afectados_por_averia())


def test_el_mensaje_lleva_el_primer_nombre():
    assert personalizar("Hola {nombre}, hay una falla.", "Ana Lopez Perez") == "Hola Ana, hay una falla."
    assert personalizar("Hola {nombre}, hay una falla.", None) == "Hola, hay una falla."
