import asyncio
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from src.application.services.ruta_cobranza_service import ordenar_ruta, ruta_cobranza


def _m(nombre, dias, total, posicion):
    return {"nombre": nombre, "dias_atraso": dias, "total": total, "posicion": posicion}


def test_con_ubicacion_va_del_mas_cercano_y_los_sin_gps_al_final():
    ruta = ordenar_ruta([
        _m("Lejos", 40, 350, (16.80, -93.10)),
        _m("Sin GPS", 90, 700, None),
        _m("Cerca", 5, 350, (16.751, -93.10)),
    ], origen=(16.75, -93.10))
    assert [m["nombre"] for m in ruta] == ["Cerca", "Lejos", "Sin GPS"]
    assert ruta[0]["distancia_m"] < 200 and ruta[2]["distancia_m"] is None


def test_sin_ubicacion_va_el_mas_atrasado_primero():
    ruta = ordenar_ruta([_m("Poco", 3, 350, None), _m("Mucho", 60, 700, None)], origen=None)
    assert [m["nombre"] for m in ruta] == ["Mucho", "Poco"]


class _Resultado:
    def __init__(self, filas):
        self.filas = filas

    def scalars(self):
        return self

    def all(self):
        return self.filas


class _DB:
    def __init__(self, facturas):
        self.facturas = facturas
        self.consulta = None

    async def execute(self, consulta):
        self.consulta = str(consulta)
        return _Resultado(self.facturas)


def test_suma_las_facturas_vencidas_de_cada_cliente():
    cliente = SimpleNamespace(id=5, nombre="Ana", cedula="A7F2", telefono="961", direccion="Calle 1",
                              estado="suspendido", latitud=None, longitud=None)
    servicio = SimpleNamespace(latitud=16.75, longitud=-93.1, direccion="Calle 1 #20")
    facturas = [
        SimpleNamespace(cliente=cliente, servicio=servicio, saldo_pendiente=Decimal("350"), fecha_vencimiento=date(2026, 9, 15)),
        SimpleNamespace(cliente=cliente, servicio=servicio, saldo_pendiente=Decimal("350"), fecha_vencimiento=date(2026, 8, 15)),
    ]
    db = _DB(facturas)

    ruta = asyncio.run(ruta_cobranza(db, hoy=date(2026, 10, 6)))

    assert ruta == [{
        "cliente_id": 5, "nombre": "Ana", "contrato": "A7F2", "telefono": "961", "direccion": "Calle 1 #20",
        "estado": "suspendido", "total": 700.0, "dias_atraso": 52, "latitud": 16.75, "longitud": -93.1,
    }]
    assert "facturas.es_promesa_activa = false" in db.consulta.lower()
