import asyncio
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from fastapi.routing import APIRoute
from sqlalchemy import UniqueConstraint

from src.application.services.billing_service import BillingService
from src.domain.schemas import ServicioCreate, ServicioResponse
from src.infrastructure.models import (
    CicloFacturacion,
    DiagnosticoSoporteModel,
    FacturaModel,
    HistorialEquipoModel,
    LecturaOpticaModel,
    OrdenServicioModel,
    PuertoNapModel,
    ServicioModel,
    TipoFacturacion,
)
from src.main import app


class _ScalarResult:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return self

    def all(self):
        return self.values

    def first(self):
        return self.values[0] if self.values else None


class _BillingDB:
    def __init__(self, servicios):
        self.resultados = iter(
            [_ScalarResult(servicios)]
            + [_ScalarResult([]) for _ in servicios]
        )
        self.facturas = []
        self.commits = 0

    async def execute(self, _statement):
        return next(self.resultados)

    def add(self, instancia):
        if isinstance(instancia, FacturaModel):
            self.facturas.append(instancia)

    async def flush(self):
        return None

    async def commit(self):
        self.commits += 1


def _servicio_facturable(servicio_id, cliente):
    return SimpleNamespace(
        id=servicio_id,
        cliente=cliente,
        cliente_id=cliente.id,
        alias=f"Casa {servicio_id}",
        direccion=f"Calle {servicio_id}",
        estado="activo",
        ciclo_facturacion=CicloFacturacion.calendario,
        tipo_facturacion=TipoFacturacion.prepago,
        proxima_facturacion=date(2026, 7, 1),
        dia_vencimiento=1,
        plantilla=SimpleNamespace(
            dia_pago=1,
            impuesto=Decimal("0"),
            dias_antes_emision=0,
            dias_tolerancia=0,
        ),
        plan=SimpleNamespace(
            nombre="50 Megas",
            precio=Decimal("500.00"),
        ),
    )


def test_emision_masiva_genera_una_factura_por_cada_servicio():
    cliente = SimpleNamespace(id=10, nombre="Cliente", telefono=None)
    db = _BillingDB(
        [
            _servicio_facturable(101, cliente),
            _servicio_facturable(102, cliente),
        ]
    )

    reporte = asyncio.run(
        BillingService(db).generar_emision_masiva(dia_objetivo=1)
    )

    assert reporte["total_procesados"] == 2
    assert reporte["facturas_generadas"] == 2
    assert {factura.servicio_id for factura in db.facturas} == {
        101,
        102,
    }
    assert {factura.cliente_id for factura in db.facturas} == {10}
    assert db.commits == 1


def test_emision_masiva_hereda_plantilla_historica_del_cliente():
    plantilla = SimpleNamespace(
        dia_pago=15,
        impuesto=Decimal("16"),
        dias_antes_emision=0,
        dias_tolerancia=3,
    )
    cliente = SimpleNamespace(
        id=10,
        nombre="Cliente",
        telefono=None,
        plantilla=plantilla,
    )
    servicio = _servicio_facturable(101, cliente)
    servicio.plantilla = None
    servicio.dia_vencimiento = None
    db = _BillingDB([servicio])

    reporte = asyncio.run(
        BillingService(db).generar_emision_masiva(dia_objetivo=15)
    )

    assert reporte["facturas_generadas"] == 1
    assert db.facturas[0].impuesto == Decimal("37.33")


def test_modelo_tecnico_queda_asociado_al_servicio():
    for modelo in (
        OrdenServicioModel,
        PuertoNapModel,
        HistorialEquipoModel,
        LecturaOpticaModel,
        DiagnosticoSoporteModel,
    ):
        assert "servicio_id" in modelo.__table__.columns

    columnas_servicio = ServicioModel.__table__.columns
    for nombre in (
        "direccion",
        "router_id",
        "plan_id",
        "onu_id",
        "caja_nap_id",
        "puerto_nap",
        "ip_asignada",
        "user_pppoe",
        "is_online",
    ):
        assert nombre in columnas_servicio

    restricciones = {
        restriccion.name
        for restriccion in ServicioModel.__table__.constraints
        if isinstance(restriccion, UniqueConstraint)
    }
    assert "uq_servicios_onu_id" in restricciones
    assert "uq_servicios_nap_puerto" in restricciones


def test_contrato_api_no_expone_password_pppoe():
    entrada = ServicioCreate(
        cliente_id=7,
        alias="Casa Centro",
        direccion="Avenida Principal 123",
    )
    assert entrada.cliente_id == 7
    assert entrada.crear_orden is True
    assert "pass_pppoe" not in ServicioResponse.model_fields


def test_rutas_multi_servicio_estan_publicadas():
    rutas = {
        (route.path, method)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    esperadas = {
        ("/servicios/", "POST"),
        ("/servicios/cliente/{cliente_id}", "GET"),
        ("/servicios/{servicio_id}/activar", "POST"),
        ("/servicios/{servicio_id}/estado", "PUT"),
        (
            "/network/diagnostico/servicios/{servicio_id}/tecnico",
            "GET",
        ),
        (
            "/network/diagnostico/servicios/{servicio_id}/ping",
            "GET",
        ),
    }
    assert esperadas <= rutas


def test_migracion_elige_un_solo_servicio_principal_por_cliente():
    migracion = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "c3d4e5f6a7b8_servicios_multi_domicilio.py"
    ).read_text(encoding="utf-8")

    assert "MIN(id) AS servicio_principal_id" in migracion
    assert '"diagnosticos_soporte"' in migracion
    assert "uq_puertos_nap_servicio_id" in migracion
