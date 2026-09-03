from decimal import Decimal
from datetime import date, timedelta
import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import Numeric

from src.application.services.finance_service import FinanceService
from src.application.services.billing_service import BillingService
from src.infrastructure.models import (
    ClienteModel,
    FacturaModel,
    PagoModel,
    PlanModel,
    ServicioModel,
    SuspensionFacturacionModel,
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


def test_invoice_keeps_cancellation_audit_fields():
    assert {
        "motivo_anulacion",
        "anulada_por_id",
        "fecha_anulacion",
        "saldo_antes_anulacion",
    }.issubset(FacturaModel.__table__.c.keys())


def test_invoice_keeps_service_day_breakdown_and_consolidation_fields():
    assert {
        "monto_servicio_original",
        "impuesto_servicio_original",
        "cargos_adicionales_total",
        "dias_con_servicio",
        "dias_sin_servicio",
        "ajuste_suspension",
    }.issubset(FacturaModel.__table__.c.keys())
    assert {
        "fecha_suspension_facturacion",
        "fecha_ultima_reactivacion",
    }.issubset(ServicioModel.__table__.c.keys())
    assert {
        "servicio_id",
        "factura_origen_id",
        "fecha_inicio",
        "fecha_fin",
        "motivo_inicio",
        "motivo_fin",
    }.issubset(SuspensionFacturacionModel.__table__.c.keys())


@pytest.mark.parametrize(
    ("precio_plan", "precio_diario", "total_esperado", "ajuste_esperado"),
    [
        ("300.00", "9.6774", "154.84", "145.16"),
        ("500.00", "16.1290", "258.06", "241.94"),
        ("725.00", "23.3871", "374.19", "350.81"),
    ],
)
def test_reactivation_uses_each_service_plan_price(
    precio_plan,
    precio_diario,
    total_esperado,
    ajuste_esperado,
):
    factura = FacturaModel(
        id=10,
        servicio_id=4,
        periodo_desde=date(2026, 8, 1),
        periodo_hasta=date(2026, 8, 31),
        dias_facturados=31,
        precio_diario=Decimal(precio_diario),
        monto_servicio_original=Decimal(precio_plan),
        impuesto_servicio_original=Decimal("0.00"),
        cargos_adicionales_total=Decimal("0.00"),
        descuento_total=Decimal("0.00"),
        fecha_vencimiento=date(2026, 8, 1),
        estado="vencida",
    )
    servicio = ServicioModel(id=4, estado="suspendido")
    intervalos = [
        SuspensionFacturacionModel(
            servicio_id=4,
            fecha_inicio=date(2026, 8, 6),
            fecha_fin=date(2026, 8, 17),
        ),
        SuspensionFacturacionModel(
            servicio_id=4,
            fecha_inicio=date(2026, 8, 26),
            fecha_fin=None,
        ),
    ]

    class Resultado:
        def __init__(self, valor):
            self.valor = valor

        def scalars(self):
            return self

        def all(self):
            return self.valor

        def scalar_one(self):
            return self.valor

    class DbFalsa:
        def __init__(self):
            self.resultados = [Resultado(intervalos), Resultado(Decimal("0.00"))]

        async def execute(self, _consulta):
            return self.resultados.pop(0)

    asyncio.run(
        FinanceService(DbFalsa()).recalcular_factura_por_suspension(
            factura,
            servicio,
            fecha_reactivacion=date(2026, 8, 29),
        )
    )

    assert factura.dias_sin_servicio == 15
    assert factura.dias_con_servicio == 16
    assert factura.total == Decimal(total_esperado)
    assert factura.ajuste_suspension == Decimal(ajuste_esperado)


@pytest.mark.parametrize(
    ("intervalos", "reactivacion", "dias_con_servicio", "dias_sin_servicio", "total"),
    [
        # Erika: corte el 6 y reactivación el 8. Sólo los días completos 6 y 7
        # quedan sin servicio; pagar el 8 no mueve el ciclo al día 8.
        ([(date(2026, 8, 6), None)], date(2026, 8, 8), 29, 2, "280.64"),
        # Natali: servicio del 1 al 9 y del 20 al 31.
        ([(date(2026, 8, 10), date(2026, 8, 19))], None, 21, 10, "203.23"),
    ],
)
def test_calendar_cycle_charges_used_and_remaining_days_without_moving_due_date(
    intervalos,
    reactivacion,
    dias_con_servicio,
    dias_sin_servicio,
    total,
):
    factura = FacturaModel(
        id=20,
        servicio_id=8,
        periodo_desde=date(2026, 8, 1),
        periodo_hasta=date(2026, 8, 31),
        dias_facturados=31,
        precio_diario=Decimal("9.6774"),
        monto_servicio_original=Decimal("300.00"),
        impuesto_servicio_original=Decimal("0.00"),
        cargos_adicionales_total=Decimal("0.00"),
        descuento_total=Decimal("0.00"),
        fecha_vencimiento=date(2026, 8, 1),
        estado="vencida",
    )
    servicio = ServicioModel(
        id=8,
        estado="suspendido",
        proxima_facturacion=date(2026, 9, 1),
    )
    registros = [
        SuspensionFacturacionModel(
            servicio_id=8,
            fecha_inicio=inicio,
            fecha_fin=fin,
        )
        for inicio, fin in intervalos
    ]

    class Resultado:
        def __init__(self, valor):
            self.valor = valor

        def scalars(self):
            return self

        def all(self):
            return self.valor

        def scalar_one(self):
            return self.valor

    class DbFalsa:
        def __init__(self):
            self.resultados = [Resultado(registros), Resultado(Decimal("0.00"))]

        async def execute(self, _consulta):
            return self.resultados.pop(0)

    asyncio.run(
        FinanceService(DbFalsa()).recalcular_factura_por_suspension(
            factura,
            servicio,
            fecha_reactivacion=reactivacion,
        )
    )

    assert factura.dias_con_servicio == dias_con_servicio
    assert factura.dias_sin_servicio == dias_sin_servicio
    assert factura.total == Decimal(total)
    assert servicio.proxima_facturacion == date(2026, 9, 1)
    if dias_sin_servicio:
        assert "Días con servicio:" in factura.descripcion
        assert "Días sin servicio y no cobrados:" in factura.descripcion


def test_full_suspended_cycle_is_closed_without_marking_it_paid():
    factura = FacturaModel(
        id=30,
        servicio_id=9,
        periodo_desde=date(2026, 7, 1),
        periodo_hasta=date(2026, 7, 31),
        dias_facturados=31,
        precio_diario=Decimal("9.6774"),
        monto_servicio_original=Decimal("300.00"),
        impuesto_servicio_original=Decimal("0.00"),
        cargos_adicionales_total=Decimal("0.00"),
        descuento_total=Decimal("0.00"),
        fecha_vencimiento=date(2026, 7, 1),
        estado="vencida",
    )
    servicio = ServicioModel(id=9, estado="suspendido")
    intervalos = [
        SuspensionFacturacionModel(
            servicio_id=9,
            fecha_inicio=date(2026, 7, 1),
            fecha_fin=date(2026, 7, 31),
        )
    ]

    class Resultado:
        def __init__(self, valor):
            self.valor = valor

        def scalars(self):
            return self

        def all(self):
            return self.valor

        def scalar_one(self):
            return self.valor

    class DbFalsa:
        def __init__(self):
            self.resultados = [
                Resultado(intervalos),
                Resultado(Decimal("0.00")),
                Resultado([]),
            ]

        async def execute(self, _consulta):
            return self.resultados.pop(0)

    asyncio.run(
        FinanceService(DbFalsa()).recalcular_factura_por_suspension(
            factura,
            servicio,
            fecha_reactivacion=date(2026, 8, 1),
        )
    )

    assert factura.dias_con_servicio == 0
    assert factura.dias_sin_servicio == 31
    assert factura.total == Decimal("0.00")
    assert factura.saldo_pendiente == Decimal("0.00")
    assert factura.estado == "sin_cargo"
    assert factura.fecha_pago_real is None
    assert factura.descripcion.startswith("Sin cargo por suspensión")


def test_automatic_normalization_waits_until_period_is_closed():
    hoy = date.today()
    pasado = FacturaModel(
        id=40,
        servicio_id=10,
        periodo_desde=hoy - timedelta(days=60),
        periodo_hasta=hoy - timedelta(days=30),
        estado="vencida",
    )
    actual = FacturaModel(
        id=41,
        servicio_id=10,
        periodo_desde=hoy,
        periodo_hasta=hoy + timedelta(days=29),
        estado="pendiente",
    )
    servicio = ServicioModel(id=10, estado="suspendido")

    class Resultado:
        def scalars(self):
            return self

        def all(self):
            return [pasado, actual]

    class DbFalsa:
        async def execute(self, _consulta):
            return Resultado()

    finanzas = FinanceService(DbFalsa())
    finanzas.recalcular_factura_por_suspension = AsyncMock()

    normalizadas = asyncio.run(
        finanzas.normalizar_facturas_suspendidas(
            servicio,
            solo_periodos_cerrados=True,
        )
    )

    assert normalizadas == [pasado]
    finanzas.recalcular_factura_por_suspension.assert_awaited_once_with(
        pasado,
        servicio,
        fecha_reactivacion=None,
    )


def test_payment_is_redirected_to_oldest_real_service_debt():
    solicitada = FacturaModel(
        id=52,
        cliente_id=7,
        servicio_id=12,
        afecta_corte=True,
        estado="pendiente",
        saldo_pendiente=Decimal("300.00"),
        fecha_vencimiento=date(2026, 9, 1),
    )
    anterior = FacturaModel(
        id=51,
        cliente_id=7,
        servicio_id=12,
        afecta_corte=True,
        estado="vencida",
        saldo_pendiente=Decimal("48.39"),
        fecha_vencimiento=date(2026, 8, 1),
    )
    servicio = ServicioModel(id=12, estado="activo")

    class Resultado:
        def scalars(self):
            return self

        def first(self):
            return anterior

    class DbFalsa:
        async def get(self, modelo, _identificador):
            if modelo is FacturaModel:
                return solicitada
            if modelo is ServicioModel:
                return servicio
            return None

        async def execute(self, _consulta):
            return Resultado()

    cobrable, original, servicio_encontrado = asyncio.run(
        BillingService(DbFalsa()).preparar_factura_cobrable(solicitada.id)
    )

    assert cobrable is anterior
    assert original is solicitada
    assert servicio_encontrado is servicio
