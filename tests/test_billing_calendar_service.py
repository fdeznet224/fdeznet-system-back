from datetime import date

import pytest

from src.application.services.billing_calendar_service import (
    BillingCalendarService,
)


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
    resultado = BillingCalendarService.calcular_fechas_servicio(
        fecha_instalacion=date(2026, 1, 31),
        meses_gratis=1,
    )

    assert resultado.fecha_fin_periodo_gratis == date(2026, 2, 28)
    assert resultado.fecha_inicio_cobro == date(2026, 3, 1)


def test_instalacion_31_de_enero_anio_bisiesto():
    resultado = BillingCalendarService.calcular_fechas_servicio(
        fecha_instalacion=date(2028, 1, 31),
        meses_gratis=1,
    )

    assert resultado.fecha_fin_periodo_gratis == date(2028, 2, 29)
    assert resultado.fecha_inicio_cobro == date(2028, 3, 1)


def test_servicio_sin_mes_gratis():
    resultado = BillingCalendarService.calcular_fechas_servicio(
        fecha_instalacion=date(2026, 7, 15),
        meses_gratis=0,
    )

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
        BillingCalendarService.calcular_fechas_servicio(
            fecha_instalacion=date(2026, 7, 15),
            meses_gratis=-1,
        )


def test_instalacion_tiene_facturacion_prepago_por_defecto():
    from src.domain.schemas import InstalacionRequest

    solicitud = InstalacionRequest(
        user_pppoe="cliente-test",
        pass_pppoe="clave-test",
    )

    assert solicitud.tipo_facturacion.value == "prepago"
    assert solicitud.ciclo_facturacion.value == "calendario"
    assert solicitud.meses_gratis == 1
    assert solicitud.fecha_instalacion is None
    assert solicitud.fecha_activacion is None
