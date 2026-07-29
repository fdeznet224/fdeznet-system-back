from decimal import Decimal

import pytest
from sqlalchemy import Numeric

from src.application.services.finance_service import FinanceService
from src.infrastructure.models import (
    ClienteModel,
    FacturaModel,
    PagoModel,
    PlanModel,
)


def test_money_uses_decimal_rounding():
    assert FinanceService.dinero("10.005") == Decimal("10.01")
    assert FinanceService.dinero(0.1) == Decimal("0.10")


def test_money_rejects_zero_and_negative_amounts():
    with pytest.raises(ValueError):
        FinanceService.dinero("0")
    with pytest.raises(ValueError):
        FinanceService.dinero("-1")


def test_payment_method_normalization_supports_bot_and_transfer():
    assert FinanceService.normalizar_metodo("BOT_AUTOPAGO") == "autovalidado"
    assert FinanceService.normalizar_metodo("Transferencia bancaria") == "transferencia"


def test_core_financial_columns_use_fixed_decimals():
    columns = [
        PlanModel.__table__.c.precio,
        ClienteModel.__table__.c.saldo_a_favor,
        FacturaModel.__table__.c.total,
        FacturaModel.__table__.c.saldo_pendiente,
        PagoModel.__table__.c.monto_total,
    ]
    assert all(isinstance(column.type, Numeric) for column in columns)


def test_payment_keeps_reversible_balance_fields():
    assert {
        "saldo_anterior",
        "saldo_posterior",
        "monto_aplicado",
        "monto_saldo_favor",
        "monto_saldo_favor_usado",
        "estado",
        "motivo_anulacion",
    }.issubset(PagoModel.__table__.c.keys())
