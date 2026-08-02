from datetime import date

from src.application.services.finance_service import calcular_fecha_maxima_promesa


def test_promesa_no_invade_siguiente_pago_dia_uno():
    assert calcular_fecha_maxima_promesa(
        date(2026, 8, 1), date(2026, 8, 6), 25
    ) == date(2026, 8, 31)


def test_promesa_dia_quince_respetar_25_dias_y_siguiente_pago():
    assert calcular_fecha_maxima_promesa(
        date(2026, 8, 15), date(2026, 8, 20), 25
    ) == date(2026, 9, 14)


def test_promesa_temprana_usa_limite_de_25_dias():
    assert calcular_fecha_maxima_promesa(
        date(2026, 8, 15), date(2026, 8, 1), 25
    ) == date(2026, 8, 26)
