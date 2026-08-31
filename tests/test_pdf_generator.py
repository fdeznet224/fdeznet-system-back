from datetime import date, datetime

from src.application.helpers.pdf_generator import (
    convertir_monto_a_texto,
    formatear_fecha_en_espanol,
)


def test_formatea_fecha_en_espanol_sin_ambiguedad():
    assert formatear_fecha_en_espanol(date(2026, 8, 30)) == (
        "30 de agosto de 2026"
    )


def test_formatea_fecha_de_pago_con_hora():
    assert formatear_fecha_en_espanol(
        datetime(2026, 8, 30, 14, 5), incluir_hora=True
    ) == "30 de agosto de 2026, 14:05 h"


def test_convierte_un_entero_como_pesos_y_no_como_centavos():
    assert convertir_monto_a_texto(50) == (
        "CINCUENTA PESOS CON CERO CENTAVOS"
    )
