import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.application.services import license_service
from src.domain.schemas import LicensePlanCreate
from src.interfaces.api.control_plane import _apply_plan, _subscription_state


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def test_demo_vencida_cambia_a_gracia_y_despues_bloquea():
    grace = SimpleNamespace(
        estado="activa",
        plan_tipo="demo",
        suscripcion_vence=_now() - timedelta(days=1),
        dias_gracia=2,
    )
    expired = SimpleNamespace(
        estado="activa",
        plan_tipo="mensual",
        suscripcion_vence=_now() - timedelta(days=5),
        dias_gracia=3,
    )

    assert _subscription_state(grace)[0] == "gracia"
    assert _subscription_state(expired) == (
        "vencida",
        "Tu mensualidad terminó. Renueva para continuar",
    )


def test_plan_se_copia_y_renueva_sobre_la_vigencia_actual():
    current_expiry = _now() + timedelta(days=10)
    installation = SimpleNamespace(
        plan_licencia_id=None,
        plan="estandar",
        plan_nombre=None,
        plan_tipo=None,
        precio_mensual=None,
        limite_clientes=None,
        limite_routers=None,
        dias_gracia=0,
        suscripcion_inicio=None,
        suscripcion_vence=current_expiry,
    )
    plan = SimpleNamespace(
        id=4,
        codigo="basico",
        nombre="Básico",
        tipo="mensual",
        precio_mensual=Decimal("499.00"),
        limite_clientes=300,
        limite_routers=2,
        dias_gracia=3,
        duracion_dias=30,
    )

    _apply_plan(installation, plan, months=1)

    assert installation.plan_nombre == "Básico"
    assert installation.limite_clientes == 300
    assert installation.suscripcion_vence >= current_expiry + timedelta(days=30)


def test_limite_de_abonados_rechaza_una_nueva_alta(monkeypatch):
    config = SimpleNamespace(licencia_limite_clientes=2)

    async def get_config(_db):
        return config

    class DB:
        async def scalar(self, _query):
            return 2

    monkeypatch.setattr(license_service, "CONTROL_PLANE_MODE", "client")
    monkeypatch.setattr(license_service, "_config", get_config)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(license_service.ensure_client_capacity(db=DB()))

    assert exc.value.status_code == 403
    assert "máximo 2 abonados" in exc.value.detail


def test_plan_permanente_elimina_la_duracion():
    plan = LicensePlanCreate(
        codigo="vitalicio",
        nombre="Vitalicio",
        tipo="permanente",
        duracion_dias=30,
    )

    assert plan.duracion_dias is None
