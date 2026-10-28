from datetime import date, datetime

from nube.panel.cuentas import (
    estado_cuenta,
    estado_respaldo,
    nuevo_vencimiento,
    poner_variable,
    segundos_hasta,
    siguiente_puerto,
    sumar_meses,
)

HOY = date(2026, 10, 28)


def test_sumar_meses_respeta_dia_de_pago_en_meses_cortos():
    assert sumar_meses(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert sumar_meses(date(2026, 2, 28), 1, dia_pago=31) == date(2026, 3, 31)
    assert sumar_meses(date(2026, 11, 15), 3) == date(2027, 2, 15)


def test_pago_adelantado_se_suma_sobre_lo_ya_pagado():
    assert nuevo_vencimiento(date(2026, 11, 15), 2, 15, HOY) == date(2027, 1, 15)


def test_pago_atrasado_cuenta_desde_hoy():
    assert nuevo_vencimiento(date(2026, 9, 15), 1, 15, HOY) == date(2026, 11, 15)
    assert nuevo_vencimiento(None, 1, 5, HOY) == date(2026, 11, 5)


def test_estados_de_la_cuenta():
    assert estado_cuenta(None, False, HOY).clave == "sin_pago"
    assert estado_cuenta(date(2026, 12, 1), False, HOY).clave == "al_corriente"
    assert estado_cuenta(date(2026, 10, 30), False, HOY).texto == "Vence en 2 días"
    assert estado_cuenta(HOY, False, HOY).texto == "Vence hoy"
    assert estado_cuenta(date(2026, 10, 25), False, HOY).clave == "gracia"
    assert estado_cuenta(date(2026, 10, 1), False, HOY).clave == "vencido"
    assert estado_cuenta(date(2027, 1, 1), True, HOY).clave == "suspendido"


def test_siguiente_puerto_libre():
    assert siguiente_puerto([], 18001) == 18001
    assert siguiente_puerto([18001, 18002, 18004], 18001) == 18003


def test_poner_variable_cambia_o_agrega_sin_tocar_lo_demas():
    contenido = "A=1\nIMAGEN_VERSION=local\nB=2\n"
    assert poner_variable(contenido, "IMAGEN_VERSION", "2.110.0") == "A=1\nIMAGEN_VERSION=2.110.0\nB=2\n"
    assert poner_variable("A=1", "C", "x") == "A=1\nC=x\n"
    assert poner_variable("A=1\n", "A", "") == "A=\n"


def test_segundos_hasta_la_hora_del_respaldo():
    assert segundos_hasta("03:00", datetime(2026, 10, 28, 2, 0)) == 3600
    assert segundos_hasta("03:00", datetime(2026, 10, 28, 3, 0)) == 24 * 3600
    assert segundos_hasta("03:00", datetime(2026, 10, 28, 23, 30)) == 3.5 * 3600


def test_estado_del_respaldo():
    ahora = datetime(2026, 10, 28, 12)
    creado = datetime(2026, 10, 1)
    assert estado_respaldo(datetime(2026, 10, 28, 3), None, "ok", creado, ahora).tono == "verde"
    assert estado_respaldo(datetime(2026, 10, 28, 3), None, "error: 403", creado, ahora).tono == "ambar"
    assert estado_respaldo(datetime(2026, 10, 26, 3), None, "ok", creado, ahora).clave == "atrasado"
    assert estado_respaldo(datetime(2026, 10, 28, 3), "disco lleno", None, creado, ahora).clave == "fallo"
    assert estado_respaldo(None, None, None, creado, ahora).clave == "sin_respaldo"
    assert estado_respaldo(None, None, None, datetime(2026, 10, 28, 9), ahora).clave == "pendiente"
