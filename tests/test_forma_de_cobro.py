"""Día fijo de pago (calendario) y día de instalación (aniversario)."""

import asyncio
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from src.application.services.billing_calendar_service import (
    BillingCalendarService,
)
from src.application.services.billing_service import BillingService
from src.domain.schemas import (
    BillingTemplateRequest,
    InstalacionRequest,
    ServicioActivacion,
    ServicioCreate,
)
from src.infrastructure.models import CicloFacturacion
from test_multi_service import _BillingDB, _servicio_facturable


def _cliente():
    return SimpleNamespace(id=10, nombre="Cliente", telefono=None, plantilla=None)


def _servicio_por_instalacion(servicio_id, cliente, activacion):
    servicio = _servicio_facturable(servicio_id, cliente)
    servicio.ciclo_facturacion = CicloFacturacion.aniversario
    servicio.fecha_activacion = activacion
    servicio.proxima_facturacion = activacion
    servicio.dia_vencimiento = activacion.day
    servicio.plantilla.ciclo_facturacion = CicloFacturacion.aniversario
    return servicio


def test_dia_fijo_usa_el_dia_de_la_plantilla():
    servicio = SimpleNamespace(
        ciclo_facturacion=CicloFacturacion.calendario, dia_vencimiento=7
    )
    plantilla = SimpleNamespace(dia_pago=15)

    assert BillingCalendarService.dia_ciclo_servicio(servicio, plantilla) == 15


def test_dia_de_instalacion_usa_el_dia_del_servicio():
    servicio = SimpleNamespace(
        ciclo_facturacion=CicloFacturacion.aniversario,
        dia_vencimiento=None,
        fecha_activacion=date(2026, 9, 30),
    )
    plantilla = SimpleNamespace(dia_pago=1)

    assert BillingCalendarService.dia_ciclo_servicio(servicio, plantilla) == 30


def test_la_eleccion_explicita_manda_sobre_la_plantilla():
    plantilla = SimpleNamespace(ciclo_facturacion=CicloFacturacion.aniversario)

    assert BillingCalendarService.resolver_ciclo(None, plantilla) == "aniversario"
    assert (
        BillingCalendarService.resolver_ciclo("calendario", plantilla)
        == "calendario"
    )
    assert BillingCalendarService.resolver_ciclo(None, None) == "calendario"


def test_sin_eleccion_los_formularios_heredan_de_la_plantilla():
    assert BillingTemplateRequest(
        nombre="Día 1", dias_antes_emision=5, dia_pago=1, dias_tolerancia=3
    ).ciclo_facturacion.value == "calendario"
    assert (
        ServicioCreate(cliente_id=1, direccion="Calle 12345").ciclo_facturacion
        is None
    )
    assert ServicioActivacion().ciclo_facturacion is None
    assert InstalacionRequest().ciclo_facturacion is None


def test_instalado_el_30_paga_cada_30_mes_completo():
    periodo = BillingCalendarService.calcular_periodo_por_dia_ciclo(
        date(2026, 9, 30), 30, Decimal("500.00")
    )

    assert periodo.es_prorrateada is False
    assert periodo.periodo_hasta == date(2026, 10, 29)
    assert periodo.subtotal == Decimal("500.00")
    assert periodo.siguiente_facturacion == date(2026, 10, 30)


def test_instalado_el_31_paga_fin_de_febrero_y_regresa_al_31():
    enero = BillingCalendarService.calcular_periodo_por_dia_ciclo(
        date(2027, 1, 31), 31, Decimal("500.00")
    )
    febrero = BillingCalendarService.calcular_periodo_por_dia_ciclo(
        enero.siguiente_facturacion, 31, Decimal("500.00")
    )

    assert enero.siguiente_facturacion == date(2027, 2, 28)
    assert febrero.es_prorrateada is False
    assert febrero.subtotal == Decimal("500.00")
    assert febrero.siguiente_facturacion == date(2027, 3, 31)


def test_emision_factura_ambas_formas_de_cobro_juntas():
    cliente = _cliente()
    dia_fijo = _servicio_facturable(101, cliente)
    por_instalacion = _servicio_por_instalacion(
        102, cliente, date(2026, 6, 30)
    )
    db = _BillingDB([dia_fijo, por_instalacion])

    reporte = asyncio.run(BillingService(db).generar_emision_masiva())

    assert reporte["facturas_generadas"] == 2
    facturas = {factura.servicio_id: factura for factura in db.facturas}
    assert facturas[101].periodo_desde == date(2026, 7, 1)
    assert facturas[101].fecha_vencimiento == date(2026, 7, 1)
    instalacion = facturas[102]
    assert instalacion.es_prorrateada is False
    assert instalacion.periodo_desde == date(2026, 6, 30)
    assert instalacion.periodo_hasta == date(2026, 7, 29)
    assert instalacion.fecha_vencimiento == date(2026, 6, 30)
    assert instalacion.total == Decimal("500.00")
    assert instalacion.ciclo_facturacion_snapshot == "aniversario"
    assert por_instalacion.proxima_facturacion == date(2026, 7, 30)


def test_emision_manual_por_dia_incluye_clientes_por_instalacion():
    cliente = _cliente()
    db = _BillingDB(
        [
            _servicio_facturable(101, cliente),
            _servicio_por_instalacion(102, cliente, date(2026, 6, 30)),
        ]
    )

    reporte = asyncio.run(
        BillingService(db).generar_emision_masiva(dia_objetivo=30)
    )

    assert reporte["facturas_generadas"] == 1
    assert db.facturas[0].servicio_id == 102
