from datetime import date, datetime

from src.application.helpers.pdf_generator import (
    construir_detalle_facturacion,
    convertir_monto_a_texto,
    formatear_fecha_en_espanol,
)


def test_construye_detalle_facturacion_con_dias_y_ajuste():
    assert construir_detalle_facturacion(
        periodo_desde=date(2026, 8, 1),
        periodo_hasta=date(2026, 8, 31),
        dias_con_servicio=29,
        dias_sin_servicio=2,
        monto_servicio_original=300,
        ajuste_suspension="19.36",
        cargos_adicionales=0,
        total_factura="280.64",
    ) == [
        ("Periodo facturado", "01/08/2026 al 31/08/2026"),
        ("Días con servicio (cobrados)", "29"),
        ("Días sin servicio (no cobrados)", "2"),
        ("Servicio antes del ajuste", "MX$300.00"),
        ("Descuento por suspensión", "-MX$19.36"),
        ("Total de la factura", "MX$280.64"),
    ]


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
