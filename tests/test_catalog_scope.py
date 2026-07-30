import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.routing import APIRoute

from src.application.services.nap_service import NapService
from src.application.services.subscription_service import SubscriptionService
from src.domain.schemas import ServicioPlanUpdate
from src.infrastructure.models import LogCronjobModel
from src.main import app


class _EmptyScalarResult:
    def scalars(self):
        return self

    def all(self):
        return []


class _NapDB:
    def __init__(self):
        self.statements = []
        self.commits = 0

    async def execute(self, statement):
        self.statements.append(statement)
        return _EmptyScalarResult()

    async def commit(self):
        self.commits += 1


class _PlanDB:
    def __init__(self, plan):
        self.plan = plan
        self.logs = []
        self.commits = 0

    async def get(self, model, identifier):
        if model.__name__ == "PlanModel" and identifier == self.plan.id:
            return self.plan
        return None

    async def commit(self):
        self.commits += 1

    def add(self, value):
        if isinstance(value, LogCronjobModel):
            self.logs.append(value)


def _servicio(router_id=1, estado="activo"):
    return SimpleNamespace(
        id=15,
        cliente_id=7,
        alias="Casa",
        estado=estado,
        router_id=router_id,
        plan_id=10,
        plan=SimpleNamespace(id=10, nombre="Básico"),
        router=SimpleNamespace(id=router_id, nombre="Nodo"),
        cliente=SimpleNamespace(nombre="Cliente"),
        user_pppoe="cliente_casa",
        pass_pppoe="secreto",
        ip_asignada="10.0.0.15",
    )


def test_catalogo_nap_combina_zona_router_y_olt():
    db = _NapDB()

    resultado = asyncio.run(
        NapService(db).listar_naps(
            zona_id=3,
            router_id=4,
            olt_id=5,
        )
    )

    assert resultado == []
    consulta = db.statements[0]
    texto = str(consulta)
    assert "cajas_nap.zona_id" in texto
    assert "cajas_nap.olt_id" in texto
    assert "olts.router_id" in texto
    assert set(consulta.compile().params.values()) == {3, 4, 5}
    assert db.commits == 1


def test_cambio_plan_rechaza_plan_de_otro_mikrotik():
    plan = SimpleNamespace(
        id=20,
        nombre="Estándar",
        router_id=2,
    )
    db = _PlanDB(plan)
    service = SubscriptionService(db)
    service.obtener = AsyncMock(return_value=_servicio(router_id=1))

    with pytest.raises(
        ValueError,
        match="no pertenece al MikroTik",
    ):
        asyncio.run(
            service.cambiar_plan(
                15,
                ServicioPlanUpdate(plan_id=20),
            )
        )

    assert db.commits == 0


def test_cambio_plan_actualiza_servicio_y_confirma_mikrotik():
    plan = SimpleNamespace(
        id=20,
        nombre="Estándar",
        router_id=1,
    )
    db = _PlanDB(plan)
    servicio = _servicio()
    service = SubscriptionService(db)
    service.obtener = AsyncMock(return_value=servicio)
    service._sincronizar_legacy_si_principal = AsyncMock()
    service._aplicar_plan_en_mikrotik = AsyncMock()

    resultado = asyncio.run(
        service.cambiar_plan(
            15,
            ServicioPlanUpdate(plan_id=20),
        )
    )

    assert servicio.plan_id == 20
    assert servicio.plan is plan
    assert resultado["mikrotik_sincronizado"] is True
    assert "confirmado en MikroTik" in resultado["mensaje"]
    service._aplicar_plan_en_mikrotik.assert_awaited_once_with(servicio)
    assert db.commits == 1


def test_cambio_plan_fallido_queda_registrado_para_reintento():
    plan = SimpleNamespace(
        id=20,
        nombre="Estándar",
        router_id=1,
    )
    db = _PlanDB(plan)
    servicio = _servicio()
    service = SubscriptionService(db)
    service.obtener = AsyncMock(return_value=servicio)
    service._sincronizar_legacy_si_principal = AsyncMock()
    service._aplicar_plan_en_mikrotik = AsyncMock(
        side_effect=RuntimeError("router fuera de línea")
    )

    resultado = asyncio.run(
        service.cambiar_plan(
            15,
            ServicioPlanUpdate(plan_id=20),
        )
    )

    assert resultado["mikrotik_sincronizado"] is False
    assert db.commits == 2
    assert len(db.logs) == 1
    assert db.logs[0].origen == "ConciliacionMikroTik"
    assert "se reintentará" in db.logs[0].mensaje


def test_rutas_y_parametros_de_catalogos_acotados_estan_publicados():
    rutas = {
        (route.path, method)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    assert ("/servicios/{servicio_id}/plan", "PUT") in rutas

    parametros_nap = {
        item["name"]
        for item in app.openapi()["paths"]["/infraestructura/naps"]["get"][
            "parameters"
        ]
    }
    assert {"zona_id", "router_id", "olt_id"} <= parametros_nap
