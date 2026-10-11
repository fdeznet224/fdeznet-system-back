"""Ruta de reparación: clientes con la potencia alta, del más cercano al técnico."""

import asyncio
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

from src.application.services.ruta_reparacion_service import ordenar_por_cercania, ruta_reparacion


def _c(nombre, rx, posicion):
    return {"nombre": nombre, "rx": rx, "posicion": posicion}


def test_con_ubicacion_va_del_mas_cercano_y_los_sin_gps_al_final():
    ruta = ordenar_por_cercania([
        _c("Lejos", -30.0, (16.80, -93.10)),
        _c("Sin GPS", -32.0, None),
        _c("Cerca", -26.0, (16.751, -93.10)),
    ], origen=(16.75, -93.10))
    assert [c["nombre"] for c in ruta] == ["Cerca", "Lejos", "Sin GPS"]
    assert ruta[0]["distancia_m"] < 200 and ruta[2]["distancia_m"] is None


def test_sin_ubicacion_va_la_peor_potencia_primero():
    ruta = ordenar_por_cercania([_c("Límite", -25.5, None), _c("Alta", -29.0, None)], origen=None)
    assert [c["nombre"] for c in ruta] == ["Alta", "Límite"]


class _Res:
    def __init__(self, filas):
        self.filas = filas

    def all(self):
        return self.filas

    def scalars(self):
        return self


class _DB:
    def __init__(self, respuestas):
        self.respuestas = respuestas

    async def execute(self, _consulta):
        return _Res(self.respuestas.pop(0))


def test_marca_el_nivel_la_caja_y_el_reporte_abierto():
    lectura = SimpleNamespace(potencia_rx_dbm=Decimal("-28.40"), fecha=datetime(2026, 10, 10, 9))
    cliente = SimpleNamespace(id=9, nombre="Victor", cedula="VM01", direccion="Centro", latitud=None, longitud=None,
                              caja_nap_id=4, puerto_nap=2)
    servicio = SimpleNamespace(direccion=None, latitud=16.7, longitud=-93.1, caja_nap_id=None, puerto_nap=None)
    otro = SimpleNamespace(id=10, nombre="Ana", cedula="AN01", direccion="Norte", latitud=None, longitud=None,
                           caja_nap_id=None, puerto_nap=None)
    db = _DB([
        [(lectura, cliente, servicio), (SimpleNamespace(potencia_rx_dbm=Decimal("-25.60"), fecha=None), otro, servicio)],
        [(SimpleNamespace(id=77, estado="asignada", cliente_id=9), "Carlos Ruiz")],
        [SimpleNamespace(id=4, nombre="P1-V-CN-2-A")],
    ])
    ruta = asyncio.run(ruta_reparacion(db, ahora=datetime(2026, 10, 10, 12)))
    victor = next(c for c in ruta if c["nombre"] == "Victor")
    assert victor["rx"] == -28.4 and victor["nivel"] == "alta"
    assert (victor["nap"], victor["puerto_nap"], victor["latitud"]) == ("P1-V-CN-2-A", 2, 16.7)
    assert victor["reporte"] == {"id": 77, "estado": "asignada", "tecnico": "Carlos Ruiz"}
    ana = next(c for c in ruta if c["nombre"] == "Ana")
    assert ana["nivel"] == "limite" and ana["reporte"] is None
    assert "posicion" not in victor
