"""Consumo de datos por día desde los contadores PPPoE del MikroTik."""

import asyncio
from datetime import date, datetime
from types import SimpleNamespace

from src.application.services.consumo_service import (
    MAX_MINUTOS_ENTRE_LECTURAS,
    ConsumoService,
    Estado,
    Muestra,
    calcular_avance,
    leer_fecha_mikrotik,
    muestras_por_usuario,
    resumen_mes,
    usuario_de_interfaz,
)
from src.infrastructure.models import ConsumoDiarioModel

AHORA = datetime(2026, 10, 19, 18, 0)


def test_lee_el_usuario_y_la_hora_de_conexion_de_la_interfaz():
    assert usuario_de_interfaz("<pppoe-Francisca De la Cruz Perez>") == "Francisca De la Cruz Perez"
    assert usuario_de_interfaz("ether1") is None
    assert leer_fecha_mikrotik("2026-10-11 22:33:38") == datetime(2026, 10, 11, 22, 33, 38)
    assert leer_fecha_mikrotik("oct/11/2026 22:33:38") == datetime(2026, 10, 11, 22, 33, 38)
    assert leer_fecha_mikrotik("") is None


def test_tx_del_mikrotik_es_lo_que_bajo_el_cliente():
    muestras = muestras_por_usuario([
        {".id": "*F1", "name": "<pppoe-Ana>", "rx-byte": "100", "tx-byte": "900", "last-link-up-time": "2026-10-19 10:00:00"},
        {".id": "*1", "name": "ether1", "rx-byte": "5", "tx-byte": "5"},
    ])
    assert list(muestras) == ["Ana"]
    assert (muestras["Ana"].rx, muestras["Ana"].tx) == (100, 900)


def test_en_la_misma_sesion_suma_lo_que_crecio_el_contador():
    estado = Estado("*F1", rx=100, tx=1000, lectura=datetime(2026, 10, 19, 17, 55))
    muestra = Muestra("*F1", rx=150, tx=4000, conectado_desde=datetime(2026, 10, 18, 9, 0))
    assert calcular_avance(estado, muestra, AHORA) == (3000, 50, 5)


def test_si_paso_mucho_entre_lecturas_no_cuenta_todo_ese_tiempo_en_linea():
    estado = Estado("*F1", rx=0, tx=0, lectura=datetime(2026, 10, 19, 12, 0))
    muestra = Muestra("*F1", rx=10, tx=20, conectado_desde=datetime(2026, 10, 18, 9, 0))
    assert calcular_avance(estado, muestra, AHORA) == (20, 10, MAX_MINUTOS_ENTRE_LECTURAS)


def test_al_reconectarse_cuenta_todo_lo_de_la_sesion_nueva():
    estado = Estado("*F1", rx=5000, tx=90000, lectura=datetime(2026, 10, 19, 17, 55))
    muestra = Muestra("*F2", rx=200, tx=800, conectado_desde=datetime(2026, 10, 19, 17, 58))
    assert calcular_avance(estado, muestra, AHORA) == (800, 200, 2)


def test_sin_lectura_previa_una_sesion_de_dias_atras_solo_es_punto_de_partida():
    muestra = Muestra("*F1", rx=10**9, tx=10**10, conectado_desde=datetime(2026, 10, 11, 22, 33))
    assert calcular_avance(None, muestra, AHORA) == (0, 0, 0)
    de_hoy = Muestra("*F1", rx=10, tx=30, conectado_desde=datetime(2026, 10, 19, 17, 50))
    assert calcular_avance(None, de_hoy, AHORA) == (30, 10, 10)


def test_contador_reiniciado_cuenta_desde_cero():
    estado = Estado("*F1", rx=5000, tx=9000, lectura=datetime(2026, 10, 19, 17, 55))
    muestra = Muestra("*F1", rx=40, tx=70, conectado_desde=None)
    assert calcular_avance(estado, muestra, AHORA) == (70, 40, 5)


def test_el_mes_trae_todos_los_dias_y_junta_los_servicios():
    filas = [
        SimpleNamespace(fecha=date(2026, 8, 15), bajada_bytes=1000, subida_bytes=100, minutos_en_linea=900),
        SimpleNamespace(fecha=date(2026, 8, 15), bajada_bytes=500, subida_bytes=50, minutos_en_linea=900),
        SimpleNamespace(fecha=date(2026, 8, 20), bajada_bytes=10, subida_bytes=0, minutos_en_linea=30),
    ]
    resumen = resumen_mes(filas, 2026, 8)
    assert len(resumen["dias"]) == 31
    assert resumen["dias"][14] == {"fecha": "2026-08-15", "bajada": 1500, "subida": 150, "minutos": 1440}
    assert (resumen["total_bajada"], resumen["dias_con_uso"]) == (1510, 2)


class _Resultado:
    def __init__(self, filas):
        self.filas = filas

    def scalars(self):
        return self

    def all(self):
        return self.filas


class _Db:
    """Devuelve routers, servicios y filas de consumo en ese orden por router."""

    def __init__(self, routers, servicios, filas):
        self.respuestas = [routers, servicios, filas]
        self.agregados = []
        self.commits = 0

    async def execute(self, _consulta):
        return _Resultado(self.respuestas.pop(0))

    def add(self, objeto):
        self.agregados.append(objeto)

    async def commit(self):
        self.commits += 1


def test_registrar_suma_al_dia_y_crea_la_fila_del_dia_nuevo():
    router = SimpleNamespace(id=3, nombre="Paraiso")
    servicios = [
        SimpleNamespace(id=11, cliente_id=11, user_pppoe="Francisca"),
        SimpleNamespace(id=12, cliente_id=12, user_pppoe="Desconectado"),
    ]
    ayer = ConsumoDiarioModel(
        servicio_id=11, cliente_id=11, fecha=date(2026, 10, 18), bajada_bytes=7, subida_bytes=1,
        minutos_en_linea=1400, sesion_mk="*F1", ultimo_rx=100, ultimo_tx=1000,
        ultima_lectura=datetime(2026, 10, 18, 23, 58),
    )
    db = _Db([router], servicios, [ayer])

    async def lector(_router):
        return [{".id": "*F1", "name": "<pppoe-Francisca>", "rx-byte": "300", "tx-byte": "5000",
                 "last-link-up-time": "2026-10-17 08:00:00"}]

    reporte = asyncio.run(ConsumoService(db, lector=lector).registrar(datetime(2026, 10, 19, 0, 3)))

    assert reporte["servicios"] == 1
    assert ayer.bajada_bytes == 7  # lo de ayer no cambia
    [hoy] = db.agregados
    assert (hoy.fecha, hoy.bajada_bytes, hoy.subida_bytes, hoy.minutos_en_linea) == (date(2026, 10, 19), 4000, 200, 5)
    assert (hoy.sesion_mk, hoy.ultimo_tx) == ("*F1", 5000)


def test_router_que_no_contesta_queda_en_el_reporte():
    db = _Db([SimpleNamespace(id=1, nombre="Vicente Guerrero")], [], [])

    async def lector(_router):
        return None

    reporte = asyncio.run(ConsumoService(db, lector=lector).registrar(AHORA))
    assert reporte == {"servicios": 0, "routers_con_error": ["Vicente Guerrero"]}
