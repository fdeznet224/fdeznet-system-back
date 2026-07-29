from pydantic import ValidationError
import pytest

from src.application.services.sync_service import (
    OrdenEstadoPayload,
    PagoFacturaPayload,
    SyncService,
)
from src.infrastructure.models import OperacionSincronizacionModel


def test_payload_hash_es_estable_ante_orden_de_claves():
    primero = SyncService.payload_hash(
        "orden_estado",
        {"orden_id": 7, "estado": "trabajando", "version": 2},
    )
    segundo = SyncService.payload_hash(
        "orden_estado",
        {"version": 2, "estado": "trabajando", "orden_id": 7},
    )

    assert primero == segundo
    assert len(primero) == 64


def test_cierre_de_orden_no_es_operacion_offline_segura():
    with pytest.raises(ValidationError):
        OrdenEstadoPayload.model_validate(
            {"orden_id": 7, "estado": "terminada", "version": 2}
        )


def test_pago_offline_valida_monto_y_metodo():
    pago = PagoFacturaPayload.model_validate(
        {
            "factura_id": 20,
            "metodo_pago": "transferencia",
            "monto_recibido": "350.50",
            "referencia": "SPEI-123",
        }
    )

    assert str(pago.monto_recibido) == "350.50"

    with pytest.raises(ValidationError):
        PagoFacturaPayload.model_validate(
            {
                "factura_id": 20,
                "metodo_pago": "saldo_favor",
                "monto_recibido": "350.50",
            }
        )


def test_modelo_operacion_conserva_respuesta_e_idempotencia():
    assert OperacionSincronizacionModel.__tablename__ == (
        "operaciones_sincronizacion"
    )
    assert OperacionSincronizacionModel.__table__.c.id.primary_key
    assert OperacionSincronizacionModel.__table__.c.payload_hash.nullable is False
    assert OperacionSincronizacionModel.__table__.c.respuesta.nullable is False
