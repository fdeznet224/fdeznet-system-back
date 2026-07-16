from datetime import date

from src.application.services.isp_billing_cycle_service import IspBillingCycleService


def test_prorrateo_plantilla_dia_15():
    calculo = IspBillingCycleService.calcular_periodo(
        fecha_base=date(2026, 8, 13),
        dia_pago=15,
        tipo_facturacion='prepago',
        precio_mensual=350,
        dias_tolerancia=5,
    )

    assert calculo.periodo_desde == date(2026, 8, 13)
    assert calculo.periodo_hasta == date(2026, 8, 14)
    assert calculo.siguiente_facturacion == date(2026, 8, 15)
    assert calculo.fecha_vencimiento == date(2026, 8, 15)
    assert calculo.fecha_limite_corte == date(2026, 8, 20)
    assert calculo.es_prorrateada is True


def test_prorrateo_plantilla_dia_1():
    calculo = IspBillingCycleService.calcular_periodo(
        fecha_base=date(2026, 8, 13),
        dia_pago=1,
        tipo_facturacion='prepago',
        precio_mensual=350,
        dias_tolerancia=5,
    )

    assert calculo.periodo_desde == date(2026, 8, 13)
    assert calculo.periodo_hasta == date(2026, 8, 31)
    assert calculo.siguiente_facturacion == date(2026, 9, 1)
    assert calculo.fecha_vencimiento == date(2026, 9, 1)
    assert calculo.fecha_limite_corte == date(2026, 9, 6)
    assert calculo.es_prorrateada is True


def test_prorrateo_plantilla_dia_10():
    calculo = IspBillingCycleService.calcular_periodo(
        fecha_base=date(2026, 8, 13),
        dia_pago=10,
        tipo_facturacion='prepago',
        precio_mensual=350,
        dias_tolerancia=3,
    )

    assert calculo.periodo_desde == date(2026, 8, 13)
    assert calculo.periodo_hasta == date(2026, 9, 9)
    assert calculo.siguiente_facturacion == date(2026, 9, 10)
    assert calculo.fecha_vencimiento == date(2026, 9, 10)
    assert calculo.fecha_limite_corte == date(2026, 9, 13)


def test_ciclo_normal_prepago_dia_15():
    calculo = IspBillingCycleService.calcular_periodo(
        fecha_base=date(2026, 8, 15),
        dia_pago=15,
        tipo_facturacion='prepago',
        precio_mensual=350,
        dias_tolerancia=5,
    )

    assert calculo.periodo_desde == date(2026, 8, 15)
    assert calculo.periodo_hasta == date(2026, 9, 14)
    assert calculo.siguiente_facturacion == date(2026, 9, 15)
    assert calculo.fecha_vencimiento == date(2026, 8, 15)
    assert calculo.fecha_limite_corte == date(2026, 8, 20)
    assert calculo.es_prorrateada is False


def test_ciclo_normal_postpago_dia_15():
    calculo = IspBillingCycleService.calcular_periodo(
        fecha_base=date(2026, 8, 15),
        dia_pago=15,
        tipo_facturacion='postpago',
        precio_mensual=350,
        dias_tolerancia=5,
    )

    assert calculo.periodo_desde == date(2026, 8, 15)
    assert calculo.periodo_hasta == date(2026, 9, 14)
    assert calculo.siguiente_facturacion == date(2026, 9, 15)
    assert calculo.fecha_vencimiento == date(2026, 9, 15)
    assert calculo.fecha_limite_corte == date(2026, 9, 20)
