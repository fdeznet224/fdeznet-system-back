"""El prorrateo se le avisa al cliente: no corta, pero se suma a su próxima factura."""
import asyncio
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from src.application.services import billing_service
from src.application.services.billing_service import BillingService
from src.application.services.notification_service import PLANTILLAS_OBLIGATORIAS


def _factura(**cambios):
    datos = dict(
        id=77, cliente_id=5, tipo_factura="prorrateo", total=Decimal("120.00"), saldo_pendiente=Decimal("120.00"),
        periodo_desde=date(2026, 11, 3), periodo_hasta=date(2026, 11, 14), fecha_vencimiento=date(2026, 11, 15),
        dias_con_servicio=12, dias_sin_servicio=0, monto_servicio_original=Decimal("120.00"),
        ajuste_suspension=0, cargos_adicionales_total=0, detalles=None, descripcion="Del 3 al 14 de noviembre",
    )
    datos.update(cambios)
    return SimpleNamespace(**datos)


def test_avisa_el_prorrateo_con_su_proxima_factura(monkeypatch):
    enviados = []

    class _Notificador:
        def __init__(self, _db):
            pass

        async def notificar(self, tipo, cliente_id, variables_extra=None, clave_dedupe=None):
            enviados.append((tipo, cliente_id, variables_extra, clave_dedupe))
            return True

    monkeypatch.setattr(billing_service, "NotificationService", _Notificador)
    assert asyncio.run(BillingService(None).avisar_prorrateo(_factura())) is True
    [(tipo, cliente, variables, clave)] = enviados
    assert (tipo, cliente, clave) == ("aviso_prorrateo", 5, "factura:77:prorrateo")
    assert variables["total_factura"] == "$120.00" and variables["fecha_proxima_factura"] == "15/11/2026"
    assert variables["periodo_desde"] == "03/11/2026" and variables["dias_con_servicio"] == "12"


def test_no_avisa_si_ya_esta_pagado_o_no_es_prorrateo(monkeypatch):
    monkeypatch.setattr(billing_service, "NotificationService", lambda _db: (_ for _ in ()).throw(AssertionError()))
    servicio = BillingService(None)
    assert asyncio.run(servicio.avisar_prorrateo(None)) is False
    assert asyncio.run(servicio.avisar_prorrateo(_factura(saldo_pendiente=Decimal("0")))) is False
    assert asyncio.run(servicio.avisar_prorrateo(_factura(tipo_factura="mensual"))) is False


def test_la_plantilla_dice_que_no_corta_y_cuando_se_cobra():
    texto = PLANTILLAS_OBLIGATORIAS["aviso_prorrateo"]
    assert "no corta tu servicio" in texto and "{fecha_proxima_factura}" in texto and "{total_factura}" in texto
