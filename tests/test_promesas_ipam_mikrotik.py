import asyncio
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.routing import APIRoute
from sqlalchemy.sql import operators, visitors

from src.application.services.billing_service import BillingService
from src.application.services.ipam_service import IPAMService
from src.application.services.notification_service import NotificationService
from src.infrastructure.mikrotik_service import MikroTikService
from src.infrastructure.whatsapp_client import whatsapp_queue
from src.main import app


class _ScalarResult:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return self

    def all(self):
        return self.values


class _OneResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


def _roles_de_ruta(path: str) -> set[str]:
    ruta = next(
        route
        for route in app.routes
        if isinstance(route, APIRoute)
        and route.path == path
        and "POST" in route.methods
    )
    roles: set[str] = set()
    for dependencia in ruta.dependant.dependencies:
        cierre = getattr(dependencia.call, "__closure__", None)
        if not cierre:
            continue
        for celda in cierre:
            if isinstance(celda.cell_contents, set):
                roles.update(celda.cell_contents)
    return roles


def test_promesas_permiten_caja_y_administracion_pero_no_tecnico():
    esperados = {"admin", "supervisor", "cajero"}

    assert _roles_de_ruta("/finanzas/promesa-pago") == esperados
    assert (
        _roles_de_ruta("/clientes/{cliente_id}/promesa-pago")
        == esperados
    )
    assert "tecnico" not in esperados


def test_promesa_se_corta_desde_el_dia_siguiente():
    class EmptyResult:
        def unique(self):
            return self

        def scalars(self):
            return self

        def all(self):
            return []

    class FakeDB:
        def __init__(self):
            self.statement = None

        async def execute(self, statement):
            self.statement = statement
            return EmptyResult()

        async def commit(self):
            return None

    db = FakeDB()
    asyncio.run(BillingService(db).procesar_cortes_automaticos())

    comparaciones = [
        nodo
        for nodo in visitors.iterate(db.statement)
        if (
            getattr(getattr(nodo, "left", None), "name", None)
            == "fecha_promesa_pago"
        )
    ]
    assert any(
        comparacion.operator is operators.lt
        and comparacion.right.value == date.today()
        for comparacion in comparaciones
    )


def test_mensaje_promesa_tiene_fallback_obligatorio(monkeypatch):
    cliente = SimpleNamespace(
        id=15,
        nombre="Cliente Prueba",
        telefono="5512345678",
        direccion=None,
        cedula="ABCD",
        ip_asignada="192.0.2.15",
        user_pppoe="cliente15",
        pass_pppoe="secreto",
        saldo_a_favor=0,
        estado="activo",
        plan=None,
        plantilla=None,
        router=None,
        onu_asignada=None,
        zona=None,
    )

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class FakeDB:
        def __init__(self):
            self.calls = 0
            self.added = None

        async def execute(self, _stmt):
            self.calls += 1
            return Result(None if self.calls == 1 else cliente)

        def add(self, value):
            self.added = value

        async def flush(self):
            return None

        async def commit(self):
            return None

    tareas = []

    async def fake_agregar_tarea(tarea):
        tareas.append(tarea)

    monkeypatch.setattr(
        whatsapp_queue,
        "agregar_tarea",
        fake_agregar_tarea,
    )

    enviado = asyncio.run(
        NotificationService(FakeDB()).notificar(
            "promesa_pago",
            cliente.id,
            variables_extra={
                "fecha_limite_promesa": "28/07/2026",
                "monto_promesa": "$500.00",
            },
        )
    )

    assert enviado is True
    assert "28/07/2026" in tareas[0]["mensaje"]
    assert "al día siguiente" in tareas[0]["mensaje"]


def test_ipam_no_devuelve_gateway_ni_ip_asignada():
    class FakeDB:
        async def execute(self, _stmt):
            return _ScalarResult(["10.20.0.2", None])

    red = SimpleNamespace(
        cidr="10.20.0.0/29",
        gateway="10.20.0.1",
    )
    disponibles = asyncio.run(
        IPAMService(FakeDB()).listar_disponibles(red)
    )

    assert disponibles == [
        "10.20.0.3",
        "10.20.0.4",
        "10.20.0.5",
        "10.20.0.6",
    ]


def test_ipam_reserva_primera_ip_libre_en_backend():
    red = SimpleNamespace(
        id=7,
        nombre="Clientes",
        cidr="192.0.2.0/29",
        gateway="192.0.2.1",
        router_id=3,
    )

    class FakeDB:
        def __init__(self):
            self.calls = 0

        async def execute(self, _stmt):
            self.calls += 1
            if self.calls == 1:
                return _OneResult(red)
            return _ScalarResult(["192.0.2.2"])

    reservada = asyncio.run(
        IPAMService(FakeDB()).reservar_para_cliente(
            red_id=7,
            router_id=3,
        )
    )

    assert reservada == "192.0.2.3"


def test_mikrotik_reactivacion_verifica_lista_y_habilita_pppoe(
    monkeypatch,
):
    servicio = MikroTikService("router", "user", "pass")
    entradas = [{".id": "*1"}]
    llamadas = []

    def fake_request(
        method,
        endpoint,
        payload=None,
        raise_on_error=False,
    ):
        llamadas.append((method, endpoint, payload, raise_on_error))
        if endpoint.startswith("/ip/firewall/address-list?"):
            return list(entradas)
        if method == "DELETE":
            entradas.clear()
            return True
        if endpoint.startswith("/ppp/secret?"):
            return [{".id": "*2"}]
        if method == "PATCH":
            return True
        raise AssertionError((method, endpoint))

    monkeypatch.setattr(servicio, "_request", fake_request)

    assert servicio.reactivar_cliente("192.0.2.10", "cliente1") is True
    assert entradas == []
    assert any(
        method == "PATCH"
        and payload == {"disabled": "false"}
        for method, _endpoint, payload, _strict in llamadas
    )


def test_mikrotik_no_confirma_reactivacion_si_ip_sigue_en_lista(
    monkeypatch,
):
    servicio = MikroTikService("router", "user", "pass")

    def fake_request(
        method,
        endpoint,
        payload=None,
        raise_on_error=False,
    ):
        if endpoint.startswith("/ip/firewall/address-list?"):
            return [{".id": "*1"}]
        if method == "DELETE":
            return True
        raise AssertionError((method, endpoint, payload, raise_on_error))

    monkeypatch.setattr(servicio, "_request", fake_request)

    with pytest.raises(RuntimeError, match="no confirmó"):
        servicio.gestionar_corte_cliente(
            "192.0.2.10",
            suspender=False,
        )
