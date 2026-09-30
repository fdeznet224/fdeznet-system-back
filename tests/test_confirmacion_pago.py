"""El cliente recibe "pago confirmado" cuando liquida lo que pagó."""

import asyncio
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

from src.application.services.billing_service import BillingService
from src.application.services.notification_service import NotificationService, quitar_aviso_de_pdf


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


def _cobrar(monkeypatch, pagos, saldo_restante):
    enviados = []

    async def notificar(self, tipo_evento, cliente_id, **kwargs):
        enviados.append((tipo_evento, kwargs))
        return True

    monkeypatch.setattr(NotificationService, "notificar", notificar)
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
    enviados = _cobrar(monkeypatch, [_pago("0")], "500.00")

    tipo, datos = enviados[0]
    assert tipo == "pago_recibido"
    assert "Saldo pendiente: $500.00" in datos["variables_extra"]["referencia"]


def test_la_confirmacion_trae_el_recibo_escrito_sin_pdf(monkeypatch):
    enviados = _cobrar(monkeypatch, [_pago("0")], "0")

    _, datos = enviados[0]
    assert "ruta_pdf" not in datos
    recibo = datos["variables_extra"]["recibo"]
    assert "#00000040" in recibo
    assert "20/09/2026 12:00" in recibo
    assert "Efectivo" in recibo
    assert "$183.33" in recibo
    assert "• Prorrateo de internet: $183.33" in recibo
    assert "Periodo: 20/09/2026 al 30/09/2026" in recibo
    assert "Próximo pago: 01/10/2026" in recibo


def test_factura_a_medias_sigue_siendo_abono(monkeypatch):
    enviados = _cobrar(monkeypatch, [_pago("83.33")], "83.33")

    assert [tipo for tipo, _ in enviados] == ["abono_recibido"]


def test_recibo_de_prorrateo_muestra_la_fecha_real_del_siguiente_pago():
    prorrateo = _prorrateo()
    mensual = SimpleNamespace(
        tipo_factura="mensual", fecha_vencimiento=date(2026, 10, 1)
    )

    assert BillingService._proximo_vencimiento(prorrateo) == date(2026, 10, 1)
    assert BillingService._proximo_vencimiento(mensual) == date(2026, 11, 1)


def test_se_quita_la_linea_que_promete_el_pdf_de_plantillas_antiguas():
    plantilla = (
        "✅ Pago confirmado\n"
        "💰 Total de la factura: $191.94\n"
        "📄 Adjunto encontrarás tu recibo oficial en PDF con el desglose completo.\n"
        "¡Gracias por tu pago!"
    )
    limpio = quitar_aviso_de_pdf(plantilla)
    assert "PDF" not in limpio
    assert limpio.splitlines() == ["✅ Pago confirmado", "💰 Total de la factura: $191.94", "¡Gracias por tu pago!"]


def test_al_quitar_el_aviso_no_quedan_renglones_vacios_de_mas():
    assert quitar_aviso_de_pdf("Hola\n\n📄 Adjunto tu recibo en PDF.\n\nGracias") == "Hola\n\nGracias"
