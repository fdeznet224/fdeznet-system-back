import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.application.services.ftth_service import FTTHService
from src.application.services.orden_service import OrdenService, TRANSICIONES
from src.infrastructure.models import ClienteModel
from src.interfaces.api.ordenes import contenido_coincide_con_mime


def test_order_workflow_allows_normal_field_sequence():
    sequence = [
        ("pendiente", "asignada"),
        ("asignada", "en_camino"),
        ("en_camino", "trabajando"),
        ("trabajando", "terminada"),
    ]
    for current, target in sequence:
        assert OrdenService.validar_transicion(current, target) is None


def test_order_workflow_rejects_skipping_to_finished():
    with pytest.raises(ValueError):
        OrdenService.validar_transicion("pendiente", "terminada")


def test_finished_and_cancelled_orders_are_terminal():
    assert TRANSICIONES["terminada"] == set()
    assert TRANSICIONES["cancelada"] == set()


def test_client_model_has_unique_ftth_constraints():
    constraint_names = {
        constraint.name
        for constraint in ClienteModel.__table__.constraints
        if constraint.name
    }
    assert "uq_clientes_onu_id" in constraint_names
    assert "uq_clientes_nap_puerto" in constraint_names


def test_optical_reading_rejects_impossible_power():
    service = FTTHService(db=None)
    client = SimpleNamespace(id=1, onu_id=1)
    with pytest.raises(ValueError):
        asyncio.run(
            service.registrar_lectura_optica(
                client,
                Decimal("-60"),
                tecnico_id=1,
            )
        )


def test_release_port_clears_assignment_and_power():
    port = SimpleNamespace(
        estado="ocupado",
        cliente_id=10,
        orden_id=20,
        potencia_instalacion_dbm=Decimal("-19.5"),
        actualizado_por_id=None,
    )
    FTTHService._liberar_registro_puerto(port, usuario_id=3)
    assert port.estado == "libre"
    assert port.cliente_id is None
    assert port.orden_id is None
    assert port.potencia_instalacion_dbm is None
    assert port.actualizado_por_id == 3


def test_evidence_content_must_match_declared_mime():
    assert contenido_coincide_con_mime(
        b"\x89PNG\r\n\x1a\nresto",
        "image/png",
    )
    assert not contenido_coincide_con_mime(
        b"<script>alert(1)</script>",
        "image/png",
    )
