from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.infrastructure.models import ComprobantePagoRevisionModel
from src.interfaces.api.whatsapp import (
    AprobarComprobanteRequest,
    RechazarComprobanteRequest,
)


def test_modelo_revision_conserva_auditoria_del_comprobante():
    columnas = ComprobantePagoRevisionModel.__table__.columns

    assert {
        "cliente_id",
        "factura_id",
        "pago_id",
        "mensaje_chat_id",
        "media_url",
        "monto_detectado",
        "folio_detectado",
        "estado",
        "motivo_revision",
        "revisado_por_id",
        "fecha_revision",
    }.issubset(columnas.keys())


def test_aprobacion_permite_corregir_cliente_monto_y_referencia():
    datos = AprobarComprobanteRequest(
        cliente_id=12,
        factura_id=40,
        monto="500.00",
        referencia="AZTECA-123",
    )

    assert datos.monto == Decimal("500.00")
    assert datos.cliente_id == 12


def test_aprobacion_rechaza_monto_cero():
    with pytest.raises(ValidationError):
        AprobarComprobanteRequest(cliente_id=12, monto=0)


def test_rechazo_exige_un_motivo_util():
    with pytest.raises(ValidationError):
        RechazarComprobanteRequest(motivo="no")
