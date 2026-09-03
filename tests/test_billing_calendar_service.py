from datetime import date

import pytest

from src.application.services.billing_calendar_service import BillingCalendarService
from src.domain.schemas import InstalacionRequest


def test_mes_gratis_instalacion_dia_15():
    resultado = BillingCalendarService.calcular_fechas_servicio(
        fecha_instalacion=date(2026, 7, 15),
        meses_gratis=1,
        ciclo_facturacion="calendario",
    )
    assert resultado.fecha_instalacion == date(2026, 7, 15)
    assert resultado.fecha_activacion == date(2026, 7, 15)
    assert resultado.fecha_fin_periodo_gratis == date(2026, 8, 15)
    assert resultado.fecha_inicio_cobro == date(2026, 8, 16)
    assert resultado.proxima_facturacion == date(2026, 8, 16)


def test_instalacion_31_de_enero():
    resultado = BillingCalendarService.calcular_fechas_servicio(date(2026, 1, 31), meses_gratis=1)
    assert resultado.fecha_fin_periodo_gratis == date(2026, 2, 28)
    assert resultado.fecha_inicio_cobro == date(2026, 3, 1)


def test_instalacion_31_de_enero_anio_bisiesto():
    resultado = BillingCalendarService.calcular_fechas_servicio(date(2028, 1, 31), meses_gratis=1)
    assert resultado.fecha_fin_periodo_gratis == date(2028, 2, 29)
    assert resultado.fecha_inicio_cobro == date(2028, 3, 1)


def test_servicio_sin_mes_gratis():
    resultado = BillingCalendarService.calcular_fechas_servicio(date(2026, 7, 15), meses_gratis=0)
    assert resultado.fecha_fin_periodo_gratis is None
    assert resultado.fecha_inicio_cobro == date(2026, 7, 15)


def test_activacion_no_puede_ser_anterior():
    with pytest.raises(ValueError, match="no puede ser anterior"):
        BillingCalendarService.calcular_fechas_servicio(
            fecha_instalacion=date(2026, 7, 15),
            fecha_activacion=date(2026, 7, 14),
            meses_gratis=1,
        )


def test_meses_gratis_no_pueden_ser_negativos():
    with pytest.raises(ValueError, match="no pueden ser negativos"):
        BillingCalendarService.calcular_fechas_servicio(date(2026, 7, 15), meses_gratis=-1)


def test_instalacion_tiene_facturacion_prepago_por_defecto():
    solicitud = InstalacionRequest(user_pppoe="cliente-test", pass_pppoe="clave-test")
    assert solicitud.tipo_facturacion.value == "prepago"
    assert solicitud.ciclo_facturacion.value == "calendario"
    assert solicitud.meses_gratis == 0


def test_instalacion_dia_3_cobra_hasta_fin_de_mes_y_conserva_ciclo():
    fechas = BillingCalendarService.calcular_fechas_servicio(
        fecha_instalacion=date(2026, 9, 3),
    )
    periodo = BillingCalendarService.calcular_periodo_por_dia_ciclo(
        periodo_desde=fechas.proxima_facturacion,
        dia_ciclo=1,
        precio_mensual=300,
    )

    assert fechas.fecha_inicio_cobro == date(2026, 9, 3)
    assert periodo.periodo_desde == date(2026, 9, 3)
    assert periodo.periodo_hasta == date(2026, 9, 30)
    assert periodo.dias_facturados == 28
    assert periodo.total == 280
    assert periodo.siguiente_facturacion == date(2026, 10, 1)
    assert BillingCalendarService.describir_dias_cobrados(
        periodo.periodo_desde,
        periodo.periodo_hasta,
    ) == (
        "Periodo cobrado: 03/09/2026 al 30/09/2026 "
        "(28 días con servicio)."
    )


def test_prorrateo_dia_1_llega_a_fin_de_mes():
    periodo = BillingCalendarService.calcular_periodo_por_dia_ciclo(date(2026, 8, 13), 1, 310)
    assert periodo.periodo_desde == date(2026, 8, 13)
    assert periodo.periodo_hasta == date(2026, 8, 31)
    assert periodo.siguiente_facturacion == date(2026, 9, 1)
    assert periodo.dias_facturados == 19
    assert periodo.dias_periodo == 31
    assert periodo.es_prorrateada is True


def test_prorrateo_dia_15_llega_al_dia_14():
    periodo = BillingCalendarService.calcular_periodo_por_dia_ciclo(date(2026, 8, 13), 15, 310)
    assert periodo.periodo_desde == date(2026, 8, 13)
    assert periodo.periodo_hasta == date(2026, 8, 14)
    assert periodo.siguiente_facturacion == date(2026, 8, 15)
    assert periodo.dias_facturados == 2
    assert periodo.dias_periodo == 31
    assert periodo.es_prorrateada is True


def test_ciclo_normal_dia_15():
    periodo = BillingCalendarService.calcular_periodo_por_dia_ciclo(date(2026, 8, 15), 15, 310)
    assert periodo.periodo_desde == date(2026, 8, 15)
    assert periodo.periodo_hasta == date(2026, 9, 14)
    assert periodo.siguiente_facturacion == date(2026, 9, 15)
    assert periodo.dias_facturados == 31
    assert periodo.dias_periodo == 31
    assert periodo.es_prorrateada is False


def test_vencimiento_prepago_y_postpago():
    periodo = BillingCalendarService.calcular_periodo_por_dia_ciclo(date(2026, 8, 15), 15, 310)
    assert BillingCalendarService.calcular_fecha_vencimiento(periodo, "prepago") == date(2026, 8, 15)
    assert BillingCalendarService.calcular_fecha_vencimiento(periodo, "postpago") == date(2026, 9, 15)


def test_postpago_no_emite_antes_de_terminar_periodo():
    periodo = BillingCalendarService.calcular_periodo_por_dia_ciclo(date(2026, 8, 15), 15, 310)
    assert BillingCalendarService.calcular_fecha_generacion(periodo, "postpago", dias_antes_emision=3) == date(2026, 9, 15)
