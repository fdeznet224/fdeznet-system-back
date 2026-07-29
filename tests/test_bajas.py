import pytest
from pydantic import ValidationError

from src.application.services.baja_service import (
    BajaService,
    ESTADOS_BAJA_ABIERTA,
)
from src.infrastructure.models import BajaServicioModel
from src.interfaces.api.bajas import BajaCrear, ConfirmarRetiro


def test_condicion_de_retorno_define_estado_de_inventario():
    assert BajaService.estado_inventario("funcional") == "DISPONIBLE"
    assert BajaService.estado_inventario("danada") == "DANADA"
    assert BajaService.estado_inventario("incompleta") == "INCOMPLETA"
    assert BajaService.estado_inventario("perdida") == "PERDIDA"


def test_condicion_de_retorno_invalida_se_rechaza():
    with pytest.raises(ValueError):
        BajaService.estado_inventario("instalada")

    with pytest.raises(ValidationError):
        ConfirmarRetiro.model_validate({"condicion": "instalada"})


def test_motivo_de_baja_es_obligatorio():
    with pytest.raises(ValidationError):
        BajaCrear.model_validate({"motivo": "no"})


def test_expediente_conserva_snapshot_operativo():
    columnas = BajaServicioModel.__table__.c
    assert columnas.ip_snapshot.nullable is True
    assert columnas.caja_nap_id_snapshot.nullable is True
    assert columnas.puerto_nap_snapshot.nullable is True
    assert columnas.proxima_facturacion_snapshot.nullable is True


def test_estados_abiertos_solo_permiten_retiro_o_sin_equipo():
    assert ESTADOS_BAJA_ABIERTA == {
        "pendiente_retiro",
        "sin_equipo",
    }
