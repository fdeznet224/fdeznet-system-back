import asyncio
from types import SimpleNamespace

import pytest
from fastapi.routing import APIRoute

from src.application.services.access_control_service import (
    verificar_acceso_cliente,
    verificar_instalacion_asignada,
)
from src.interfaces.api.naps import PuertoNapOcupadoResponse
from src.main import app


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _FakeDB:
    def __init__(self, value):
        self.value = value
        self.calls = 0

    async def execute(self, _statement):
        self.calls += 1
        return _ScalarResult(self.value)


def _route(path: str, method: str) -> APIRoute:
    return next(
        route
        for route in app.routes
        if isinstance(route, APIRoute)
        and route.path == path
        and method in route.methods
    )


def _roles(path: str, method: str = "GET") -> set[str]:
    roles: set[str] = set()

    def walk(dependant):
        for dependency in dependant.dependencies:
            closure = getattr(dependency.call, "__closure__", None)
            if closure:
                for cell in closure:
                    if isinstance(cell.cell_contents, set):
                        roles.update(cell.cell_contents)
            walk(dependency)

    walk(_route(path, method).dependant)
    return roles


@pytest.mark.parametrize(
    ("path", "method", "expected"),
    [
        (
            "/clientes/",
            "GET",
            {"admin", "supervisor", "tecnico"},
        ),
        (
            "/clientes/listado-completo-unificado",
            "GET",
            {"admin", "supervisor"},
        ),
        (
            "/clientes/{dato}/portal",
            "GET",
            {"admin", "supervisor", "tecnico"},
        ),
        (
            "/network/routers/",
            "GET",
            {"admin", "supervisor"},
        ),
        (
            "/inventario/",
            "GET",
            {"admin", "supervisor", "tecnico"},
        ),
        (
            "/whatsapp/no-leidos",
            "GET",
            {"admin", "supervisor"},
        ),
        (
            "/whatsapp/chat/{cliente_id}",
            "GET",
            {"admin", "supervisor", "tecnico"},
        ),
    ],
)
def test_endpoints_operativos_declaran_roles(path, method, expected):
    assert _roles(path, method) == expected


def test_tecnico_no_puede_consultar_cliente_no_asignado():
    db = _FakeDB(None)
    tecnico = SimpleNamespace(id=9, rol="tecnico")

    with pytest.raises(PermissionError):
        asyncio.run(verificar_acceso_cliente(db, tecnico, 100))


def test_tecnico_puede_consultar_cliente_asignado():
    db = _FakeDB(100)
    tecnico = SimpleNamespace(id=9, rol="tecnico")

    assert (
        asyncio.run(verificar_acceso_cliente(db, tecnico, 100))
        is None
    )
    assert db.calls == 1


def test_administracion_no_necesita_asignacion_tecnica():
    db = _FakeDB(None)
    administrador = SimpleNamespace(id=1, rol="admin")

    assert (
        asyncio.run(
            verificar_acceso_cliente(db, administrador, 100)
        )
        is None
    )
    assert db.calls == 0


def test_tecnico_necesita_orden_abierta_para_instalar():
    db = _FakeDB(None)
    tecnico = SimpleNamespace(id=9, rol="tecnico")

    with pytest.raises(PermissionError):
        asyncio.run(
            verificar_instalacion_asignada(db, tecnico, 100)
        )


def test_respuesta_nap_no_expone_credenciales_del_cliente():
    fields = set(PuertoNapOcupadoResponse.model_fields)

    assert fields == {"id", "nombre", "puerto_nap", "cedula"}
    assert "user_pppoe" not in fields
    assert "pass_pppoe" not in fields
