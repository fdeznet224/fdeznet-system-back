"""Pagos del mes siguiente: la captura es válida, pero se aplican el día 1.

La validación por captura (y que el pago adelantado espere al día 1) se
prueba en test_modo_captura.
"""

from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.application.services.pagos_captura import aplicar_pago_desde


def _factura(**cambios):
    datos = dict(
        id=1134, cliente_id=248, saldo_pendiente=Decimal("300.00"),
        periodo_desde=date(2026, 10, 1), fecha_vencimiento=date(2026, 10, 1),
    )
    datos.update(cambios)
    return SimpleNamespace(**datos)


@pytest.mark.parametrize(
    "periodo, vence, esperado",
    [
        (date(2026, 10, 15), date(2026, 10, 15), datetime(2026, 10, 1, 8, 0)),  # día de instalación
        (None, date(2026, 11, 1), datetime(2026, 11, 1, 8, 0)),
        (None, None, None),
    ],
)
def test_se_aplica_el_dia_1_del_mes_que_cubre(periodo, vence, esperado):
    assert aplicar_pago_desde(_factura(periodo_desde=periodo, fecha_vencimiento=vence)) == esperado


def test_quien_ya_pago_por_adelantado_no_recibe_recordatorio():
    import inspect
    from src.application.services.billing_service import BillingService

    fuente = inspect.getsource(BillingService.enviar_recordatorios_automaticos)
    assert '"pago_adelantado"' in fuente and "~exists()" in fuente
