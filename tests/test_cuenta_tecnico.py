from datetime import date
from decimal import Decimal

from src.application.services.cuenta_tecnico_service import explicar_estado

VENCIDO = {"concepto": "Mensualidad Octubre", "monto": 300.0, "vence": "25/09/2026", "corte": None, "vencido": True}
POR_VENCER = {"concepto": "Mensualidad Noviembre", "monto": 300.0, "vence": "15/11/2026", "corte": None, "vencido": False}


def test_suspendido_dice_cuanto_debe_y_desde_cuando():
    texto = explicar_estado("suspendido", Decimal("300"), [VENCIDO], date(2026, 10, 1), None)
    assert texto.startswith("Suspendido desde el 01/10/2026 por adeudo de $300")
    assert "Mensualidad Octubre" in texto and "promesa de pago" in texto


def test_activo_con_promesa_al_corriente_o_por_vencer():
    assert "promesa de pago para el 10/10/2026" in explicar_estado("activo", Decimal("300"), [VENCIDO], None, {"fecha": "10/10/2026"})
    assert explicar_estado("activo", Decimal("0"), [], None, None) == "Al corriente, sin adeudos."
    assert "vence el 15/11/2026" in explicar_estado("activo", Decimal("300"), [POR_VENCER], None, None)
    assert "puede suspenderse" in explicar_estado("activo", Decimal("300"), [VENCIDO], None, None)


def test_suspendido_sin_adeudo_lo_revisa_administracion():
    assert "administración" in explicar_estado("suspendido", Decimal("0"), [], None, None)
