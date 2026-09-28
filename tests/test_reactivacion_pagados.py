import asyncio
from datetime import date
from types import SimpleNamespace

from sqlalchemy.sql import operators, visitors

from src.application.services.billing_service import BillingService
from src.infrastructure.models import LogCronjobModel


class _Resultado:
    def __init__(self, valores=None, escalar=0):
        self.valores = valores or []
        self.escalar = escalar

    def unique(self):
        return self

    def scalars(self):
        return self

    def all(self):
        return self.valores

    def scalar_one(self):
        return self.escalar


def _comparaciones(statement, columna):
    return [
        nodo
        for nodo in visitors.iterate(statement)
        if getattr(getattr(nodo, "left", None), "name", None) == columna
    ]


def test_reactivacion_solo_cuenta_deuda_ya_vencida():
    """Una mensualidad que aún no vence no debe mantener el corte."""

    class DB:
        statement = None

        async def execute(self, statement):
            self.statement = statement
            return _Resultado(escalar=0)

    db = DB()
    factura = SimpleNamespace(servicio_id=7, cliente_id=3)

    tiene_deuda = asyncio.run(
        BillingService(db)._servicio_tiene_deuda_pendiente(factura)
    )

    assert tiene_deuda is False
    assert any(
        c.operator is operators.lt and c.right.value == date.today()
        for c in _comparaciones(db.statement, "fecha_limite_corte")
    )
    assert any(
        c.operator is operators.lt and c.right.value == date.today()
        for c in _comparaciones(db.statement, "fecha_promesa_pago")
    )
    assert any(
        c.operator is operators.ne and c.right.value == "prorrateo"
        for c in _comparaciones(db.statement, "tipo_factura")
    )


class _DBBarrido:
    def __init__(self, servicios, clientes):
        self.servicios = servicios
        self.clientes = clientes
        self.logs = []
        self.commits = 0

    async def execute(self, _statement):
        return _Resultado(self.servicios)

    async def get(self, _model, item_id):
        return self.clientes[item_id]

    def add(self, value):
        if isinstance(value, LogCronjobModel):
            self.logs.append(value)

    async def commit(self):
        self.commits += 1


def _preparar(monkeypatch, db, con_deuda=(), mikrotik_falla=()):
    service = BillingService(db)
    llamadas = {"reactivados": [], "cerrados": [], "cargos": []}

    async def deuda(referencia, excluir_factura_id=None):
        return referencia.servicio_id in con_deuda

    async def reactivar(servicio):
        llamadas["reactivados"].append(servicio.id)
        return servicio.id not in mikrotik_falla

    async def cerrar(servicio, fecha, motivo):
        llamadas["cerrados"].append((servicio.id, fecha, motivo))

    async def sincronizar(cliente_id):
        db.clientes[cliente_id].estado = "activo"

    async def cargo(cliente, servicio, origen):
        llamadas["cargos"].append((servicio.id, origen))

    monkeypatch.setattr(service, "_servicio_tiene_deuda_pendiente", deuda)
    monkeypatch.setattr(service, "_reactivar_en_mikrotik", reactivar)
    monkeypatch.setattr(service, "_cerrar_suspension_facturacion", cerrar)
    monkeypatch.setattr(service, "_sincronizar_estado_cliente", sincronizar)
    monkeypatch.setattr(service, "_registrar_cargo_reconexion", cargo)
    return service, llamadas


def _servicio(servicio_id, cliente_id):
    return SimpleNamespace(
        id=servicio_id,
        cliente_id=cliente_id,
        estado="suspendido",
        ultima_reactivacion_origen=None,
        ultima_reactivacion_en=None,
    )


def test_barrido_reactiva_solo_servicios_sin_deuda_vencida(monkeypatch):
    pagado = _servicio(1, 10)
    moroso = _servicio(2, 20)
    clientes = {
        10: SimpleNamespace(id=10, nombre="Pagado", telefono=None, estado="suspendido"),
        20: SimpleNamespace(id=20, nombre="Moroso", telefono=None, estado="suspendido"),
    }
    db = _DBBarrido([pagado, moroso], clientes)
    service, llamadas = _preparar(monkeypatch, db, con_deuda={2})

    reporte = asyncio.run(service.reactivar_servicios_sin_deuda())

    assert reporte == {"revisados": 2, "reactivados": 1, "errores": 0}
    assert llamadas["reactivados"] == [1]
    assert pagado.estado == "activo"
    assert pagado.ultima_reactivacion_origen == "automatico"
    assert clientes[10].estado == "activo"
    assert llamadas["cerrados"] == [(1, date.today(), "pago")]
    assert llamadas["cargos"] == [(1, "pago")]
    assert moroso.estado == "suspendido"


def test_barrido_no_marca_activo_si_mikrotik_falla(monkeypatch):
    servicio = _servicio(3, 30)
    clientes = {
        30: SimpleNamespace(id=30, nombre="Sin router", telefono=None, estado="suspendido"),
    }
    db = _DBBarrido([servicio], clientes)
    service, llamadas = _preparar(monkeypatch, db, mikrotik_falla={3})

    reporte = asyncio.run(service.reactivar_servicios_sin_deuda())

    assert reporte == {"revisados": 1, "reactivados": 0, "errores": 1}
    assert servicio.estado == "suspendido"
    assert llamadas["cerrados"] == []
    assert llamadas["cargos"] == []
    assert db.logs[0].nivel == "ERROR"


def test_factura_pagada_liquida_internet_aunque_el_concepto_no_se_ajusto():
    """Caso real: factura recalculada a 338.33 por un día suspendido,
    concepto de internet todavía en 350 y 11.67 de remanente."""
    from decimal import Decimal

    concepto = SimpleNamespace(
        id=25, saldo_pendiente=Decimal("11.67"), estado="abonado"
    )

    class DB:
        def __init__(self):
            self.llamadas = 0

        async def execute(self, _statement):
            self.llamadas += 1
            if self.llamadas == 1:
                return _Resultado([concepto])
            return _Resultado(escalar=1)

        def add(self, _value):
            pass

    factura = SimpleNamespace(
        id=939, saldo_pendiente=Decimal("0.00"), afecta_corte=True
    )
    pago = SimpleNamespace(id=931, monto_aplicado=Decimal("0.00"))

    liquidado = asyncio.run(
        BillingService(DB())._distribuir_pago_en_conceptos(
            factura, pago, None
        )
    )

    assert liquidado is True
    assert factura.afecta_corte is False
