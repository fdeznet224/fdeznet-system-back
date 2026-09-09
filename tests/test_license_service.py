import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.application.services import license_service
from src.application.services.license_service import update_available


def test_detecta_actualizacion_semantica_disponible():
    assert update_available("2.3.0", "2.4.0") is True
    assert update_available("2.3.0", "2.3.1") is True


def test_no_marca_version_igual_o_anterior():
    assert update_available("2.3.0", "2.3.0") is False
    assert update_available("2.3.0", "2.2.9") is False
    assert update_available("2.3.0", None) is False


def test_version_invalida_no_provoca_actualizacion():
    assert update_available("desarrollo", "2.4.0") is False


def test_licencia_suspendida_bloquea_operacion(monkeypatch):
    async def config(_db):
        return SimpleNamespace(
            licencia_estado="suspendida",
            licencia_mensaje="Licencia suspendida por el proveedor",
        )

    monkeypatch.setattr(license_service, "CONTROL_PLANE_MODE", "client")
    monkeypatch.setattr(license_service, "INSTALLATION_ID", "installation-id")
    monkeypatch.setattr(license_service, "LICENSE_KEY", "license-key")
    monkeypatch.setattr(license_service, "_config", config)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(license_service.require_valid_license(db=object()))

    assert exc.value.status_code == 403


def test_licencia_activa_permite_operacion(monkeypatch):
    async def config(_db):
        return SimpleNamespace(licencia_estado="activa", licencia_mensaje="")

    monkeypatch.setattr(license_service, "CONTROL_PLANE_MODE", "client")
    monkeypatch.setattr(license_service, "INSTALLATION_ID", "installation-id")
    monkeypatch.setattr(license_service, "LICENSE_KEY", "license-key")
    monkeypatch.setattr(license_service, "_config", config)

    assert asyncio.run(license_service.require_valid_license(db=object())) is None
