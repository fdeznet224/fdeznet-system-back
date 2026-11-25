"""Pago por adelantado: cotizar los meses, emitirlos al pagar y aplicar el pago."""

import asyncio
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from src.application.services.billing_service import BillingService
from test_multi_service import _BillingDB, _servicio_facturable


def test_la_emision_adelantada_no_espera_su_fecha_ni_avisa():
    cliente = SimpleNamespace(id=10, nombre="Cliente", telefono="9610000000")
    servicio = _servicio_facturable(101, cliente)
    servicio.proxima_facturacion = date(2027, 3, 1)

    normal = asyncio.run(BillingService(_BillingDB([servicio])).generar_emision_masiva(servicio_id=101))
    assert normal["facturas_generadas"] == 0 and normal["omitidos_no_toca_emitir"] == 1

    db = _BillingDB([servicio])
    reporte = asyncio.run(BillingService(db).generar_emision_masiva(servicio_id=101, adelantar=True))
    assert reporte["facturas_generadas"] == 1 and reporte["mensajes_enviados"] == 0
    assert db.facturas[0].periodo_desde == date(2027, 3, 1) and db.facturas[0].total == Decimal("500.00")
    assert servicio.proxima_facturacion == date(2027, 4, 1)


def test_cotiza_los_meses_que_siguen(monkeypatch):
    cliente = SimpleNamespace(id=10, nombre="Cliente", telefono=None, plan=None, plantilla=None)
    servicio = _servicio_facturable(101, cliente)
    servicio.proxima_facturacion = date(2026, 12, 1)

    async def servicios(_self, _cliente_id):
        return [servicio]

    monkeypatch.setattr(BillingService, "_servicios_para_adelantar", servicios)
    cotizacion = asyncio.run(BillingService(_BillingDB([])).cotizar_adelanto(10, 3))
    assert cotizacion["total"] == Decimal("1500.00")
    assert [d["mes"] for d in cotizacion["detalle"]] == ["Diciembre 2026", "Enero 2027", "Febrero 2027"]
    assert servicio.proxima_facturacion == date(2026, 12, 1)  # cotizar no cambia nada


def test_emite_solo_los_meses_que_alcanza_a_pagar(monkeypatch):
    emitidos = []

    async def cotizar(_self, _cliente_id, meses):
        return {"meses": 1, "total": Decimal("500.00"), "detalle": [{"servicio_id": 101, "mes": "x", "total": Decimal("500")}]}

    async def emitir(_self, servicio_id=None, adelantar=False, dia_objetivo=None):
        emitidos.append((servicio_id, adelantar))
        return {}

    monkeypatch.setattr(BillingService, "cotizar_adelanto", cotizar)
    monkeypatch.setattr(BillingService, "generar_emision_masiva", emitir)
    billing = BillingService(SimpleNamespace())
    assert asyncio.run(billing.emitir_adelanto(10, Decimal("1100"), 3)) == 2
    assert emitidos == [(101, True), (101, True)]
    emitidos.clear()
    assert asyncio.run(billing.emitir_adelanto(10, Decimal("5000"), 3)) == 3


def test_el_pago_cubre_lo_que_debe_y_con_lo_demas_adelanta(monkeypatch):
    llamadas = {}

    class _DB:
        async def execute(self, _sentencia):
            return SimpleNamespace(scalar_one_or_none=lambda: None)

    async def estado(_self, _cliente_id):
        return {"total": Decimal("300.00")}

    async def emitir(_self, cliente_id, disponible, meses_max):
        llamadas["emitir"] = (cliente_id, disponible, meses_max)
        return 2

    async def pagar(_self, cliente_id, usuario, metodo, monto, referencia=None, clave_idempotencia=None):
        llamadas["pago"] = (cliente_id, metodo, monto, clave_idempotencia)
        return {"pago_ids": [1]}

    monkeypatch.setattr(BillingService, "estado_cuenta_cliente", estado)
    monkeypatch.setattr(BillingService, "emitir_adelanto", emitir)
    monkeypatch.setattr(BillingService, "registrar_pago_cliente", pagar)
    asyncio.run(BillingService(_DB()).registrar_pago_con_adelanto(
        10, None, "codi", Decimal("1300"), meses_adelanto=2, clave_idempotencia="codi:TX"))
    assert llamadas["emitir"] == (10, Decimal("1000.00"), 2)
    assert llamadas["pago"] == (10, "codi", Decimal("1300"), "codi:TX")


def test_el_primer_mes_incluye_los_cargos_pendientes(monkeypatch):
    """Las fichas cargadas a la siguiente factura entran en el primer mes adelantado."""
    cliente = SimpleNamespace(id=10, nombre="Cliente", telefono=None, plan=None, plantilla=None)
    servicio = _servicio_facturable(101, cliente)
    servicio.proxima_facturacion = date(2026, 12, 1)
    fichas = [SimpleNamespace(servicio_id=None, saldo_pendiente=Decimal("10.00"), concepto="Ficha 24 horas")
              for _ in range(2)]

    async def servicios(_self, _cliente_id):
        return [servicio]

    monkeypatch.setattr(BillingService, "_servicios_para_adelantar", servicios)

    class _DB(_BillingDB):
        def __init__(self):
            super().__init__([])
            from test_multi_service import _ScalarResult
            self.resultados = iter([_ScalarResult(fichas), _ScalarResult([])])

    cotizacion = asyncio.run(BillingService(_DB()).cotizar_adelanto(10, 2))
    assert [d["total"] for d in cotizacion["detalle"]] == [Decimal("520.00"), Decimal("500.00")]
    assert cotizacion["detalle"][0]["incluye"] == ["Ficha 24 horas"] and "incluye" not in cotizacion["detalle"][1]
    assert cotizacion["total"] == Decimal("1020.00")
