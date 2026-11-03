from datetime import date, datetime

from nube.panel.cuentas import (
    estado_demo,
    limpiar_texto,
    siguiente_paso_demo,
    slug_para,
    telefono_mx,
    aviso_al_isp,
    debe_suspenderse,
    dia_de_corte,
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


def test_dia_de_corte_despues_de_la_gracia_y_la_prorroga():
    assert dia_de_corte(None, 5) is None
    assert dia_de_corte(date(2026, 11, 10), 5) == date(2026, 11, 16)
    assert dia_de_corte(date(2026, 11, 10), 5, date(2026, 11, 20)) == date(2026, 11, 21)
    assert dia_de_corte(date(2026, 11, 10), 5, date(2026, 11, 1)) == date(2026, 11, 16)  # prórroga vieja


def test_debe_suspenderse():
    pagado = date(2026, 11, 10)
    assert not debe_suspenderse(pagado, False, True, None, date(2026, 11, 15), 5)  # último día de gracia
    assert debe_suspenderse(pagado, False, True, None, date(2026, 11, 16), 5)
    assert not debe_suspenderse(pagado, True, True, None, date(2026, 11, 16), 5)  # ya suspendido
    assert not debe_suspenderse(pagado, False, False, None, date(2026, 11, 16), 5)  # corte desactivado
    assert not debe_suspenderse(pagado, False, True, date(2026, 11, 18), date(2026, 11, 18), 5)
    assert not debe_suspenderse(None, False, True, None, date(2026, 11, 16), 5)  # sin pagos


def test_aviso_al_isp_antes_y_despues_de_vencer():
    hoy = date(2026, 11, 2)
    assert aviso_al_isp(date(2026, 11, 20), False, date(2026, 11, 26), hoy) is None  # lejos
    assert aviso_al_isp(None, False, None, hoy) is None  # sin pagos
    assert aviso_al_isp(date(2026, 11, 1), True, None, hoy) is None  # ya suspendido

    por_vencer = aviso_al_isp(date(2026, 11, 5), False, date(2026, 11, 11), hoy, "09:00", contacto="WhatsApp 55 1234")
    assert por_vencer["tono"] == "ambar"
    assert por_vencer["mensaje"] == ("Tu mensualidad de WisPay vence el 5 de noviembre. Si no se registra tu pago, "
                                     "el sistema se suspende el 11 de noviembre. Para pagar: WhatsApp 55 1234.")
    assert "vence mañana" in aviso_al_isp(date(2026, 11, 3), False, None, hoy)["mensaje"]

    vencido = aviso_al_isp(date(2026, 10, 28), False, date(2026, 11, 3), hoy, "09:00")
    assert vencido["tono"] == "rojo"
    assert vencido["mensaje"] == ("Tu mensualidad de WisPay venció el 28 de octubre. "
                                  "El sistema se suspende mañana si no se registra tu pago.")
    hoy_corte = aviso_al_isp(date(2026, 10, 27), False, date(2026, 11, 2), hoy, "09:00")
    assert "se suspende hoy a las 09:00" in hoy_corte["mensaje"]
    sin_corte = aviso_al_isp(date(2026, 10, 1), False, None, hoy)
    assert "Regulariza tu pago" in sin_corte["mensaje"]


def test_slug_desde_el_nombre_de_la_empresa():
    assert slug_para("Red Rápida S.A. de C.V.", set()) == "red-rapida"
    assert slug_para("Red Rápida", {"red-rapida"}) == "red-rapida-2"
    assert slug_para("Panel", set()) == "panel-2"  # reservado
    assert slug_para("2000 Net", set()) == "isp-2000-net"


def test_telefono_de_mexico():
    assert telefono_mx("961 363 2496") == telefono_mx("+52 1 961-363-2496") == "529613632496"
    assert telefono_mx("12345") is None and telefono_mx("0123456789") is None


def test_texto_publico_sin_lo_que_rompe_un_env():
    assert limpiar_texto('Red "$(x)" `y`\nNet') == "Red (x) y Net"


def test_pasos_del_demo():
    hasta = date(2026, 12, 2)
    assert siguiente_paso_demo(hasta, set(), date(2026, 11, 20), 15) is None
    assert siguiente_paso_demo(hasta, set(), date(2026, 11, 27), 15) == "aviso"
    assert siguiente_paso_demo(hasta, {"aviso"}, date(2026, 12, 2), 15) is None  # último día gratis
    assert siguiente_paso_demo(hasta, {"aviso"}, date(2026, 12, 3), 15) == "fin"
    assert siguiente_paso_demo(hasta, set(), date(2026, 12, 18), 15) == "borrar"  # se saltó días: borra
    assert estado_demo(hasta, False, date(2026, 11, 2), 15).texto == "Demo gratis hasta 02/12/2026"
    assert estado_demo(hasta, True, date(2026, 12, 5), 15).clave == "demo_fin"
