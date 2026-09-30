"""El cliente recibe "pago confirmado" cuando liquida lo que pagó."""

import asyncio
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

import src.application.services.billing_service as billing_module
from src.application.services.billing_service import BillingService
from src.application.services.notification_service import NotificationService


class _CobroDB:
    def __init__(self, pagos, factura):
        self.pagos = pagos
        self.factura = factura

    async def execute(self, statement):
        if "FROM pagos" in str(statement) and "facturas_conceptos" not in str(
            statement
        ):
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: self.pagos)
            )
        return SimpleNamespace(all=lambda: [("Prorrateo de internet", Decimal("183.33"))])

    async def get(self, _modelo, _id):
        return self.factura if _modelo.__name__ == "FacturaModel" else None


def _prorrateo():
    return SimpleNamespace(
        id=40,
        tipo_factura="prorrateo",
        fecha_vencimiento=date(2026, 10, 1),
        descripcion="Periodo cobrado: 20/09/2026 al 30/09/2026",
        detalles="Prorrateo Internet",
        periodo_desde=date(2026, 9, 20),
        periodo_hasta=date(2026, 9, 30),
        dias_con_servicio=11,
        dias_sin_servicio=0,
        monto_servicio_original=Decimal("183.33"),
        ajuste_suspension=0,
        cargos_adicionales_total=0,
        total=Decimal("183.33"),
    )


def _pago(saldo_posterior):
    return SimpleNamespace(
        id=900,
        factura_id=40,
        saldo_posterior=Decimal(saldo_posterior),
        fecha_pago=datetime(2026, 9, 20, 12, 0),
        metodo_pago="efectivo",
    )


def _cobrar(monkeypatch, pagos, saldo_restante, recibo=None):
    enviados = []

    async def notificar(self, tipo_evento, cliente_id, **kwargs):
        enviados.append((tipo_evento, kwargs))
        return True

    async def generar(**_datos):
        if recibo is None:
            raise OSError("disco lleno")
        return recibo

    monkeypatch.setattr(NotificationService, "notificar", notificar)
    monkeypatch.setattr(billing_module, "generar_recibo_pdf", generar)
    cliente = SimpleNamespace(id=5, nombre="Cliente", telefono="5512345678")
    asyncio.run(
        BillingService(_CobroDB(pagos, _prorrateo()))._notificar_cobro_total(
            cliente,
            [900],
            Decimal("183.33"),
            Decimal(saldo_restante),
            False,
            None,
        )
    )
    return enviados


def test_prorrateo_liquidado_confirma_aunque_haya_otra_factura(monkeypatch):
    enviados = _cobrar(monkeypatch, [_pago("0")], "500.00", "/tmp/r.pdf")

    tipo, datos = enviados[0]
    assert tipo == "pago_recibido"
    assert datos["ruta_pdf"] == "/tmp/r.pdf"
    assert "Saldo pendiente: $500.00" in datos["variables_extra"]["referencia"]


def test_factura_a_medias_sigue_siendo_abono(monkeypatch):
    enviados = _cobrar(monkeypatch, [_pago("83.33")], "83.33", "/tmp/r.pdf")

    assert [tipo for tipo, _ in enviados] == ["abono_recibido"]


def test_si_el_recibo_falla_la_confirmacion_sale_sin_pdf(monkeypatch):
    enviados = _cobrar(monkeypatch, [_pago("0")], "0", recibo=None)

    tipo, datos = enviados[0]
    assert tipo == "pago_recibido"
    assert datos["ruta_pdf"] is None


def test_recibo_de_prorrateo_muestra_la_fecha_real_del_siguiente_pago():
    prorrateo = _prorrateo()
    mensual = SimpleNamespace(
        tipo_factura="mensual", fecha_vencimiento=date(2026, 10, 1)
    )

    assert BillingService._proximo_vencimiento(prorrateo) == date(2026, 10, 1)
    assert BillingService._proximo_vencimiento(mensual) == date(2026, 11, 1)
