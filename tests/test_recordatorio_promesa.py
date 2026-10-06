import asyncio
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import src.application.services.billing_service as billing_mod
from src.application.services.billing_service import BillingService
from src.application.services.notification_service import PLANTILLAS_OBLIGATORIAS


class _Resultado:
    def __init__(self, filas):
        self.filas = filas

    def scalars(self):
        return self

    def unique(self):
        return self

    def all(self):
        return self.filas


class _DB:
    def __init__(self, facturas):
        self.facturas = facturas
        self.consultas = []

    async def execute(self, consulta):
        self.consultas.append(str(consulta))
        return _Resultado(self.facturas)

    async def commit(self):
        return None


def test_el_dia_de_la_promesa_se_le_recuerda_con_su_saldo(monkeypatch):
    enviados = []

    class _Notificador:
        def __init__(self, _db):
            pass

        async def notificar(self, **kwargs):
            enviados.append(kwargs)

    monkeypatch.setattr(billing_mod, "NotificationService", _Notificador)
    facturas = [
        SimpleNamespace(id=10, saldo_pendiente=Decimal("350"), cliente=SimpleNamespace(id=5, nombre="Ana", telefono="9611111111")),
        SimpleNamespace(id=11, saldo_pendiente=Decimal("350"), cliente=SimpleNamespace(id=6, nombre="Sin tel", telefono=None)),
    ]
    servicio = BillingService(_DB(facturas))
    monkeypatch.setattr(servicio, "estado_cuenta_cliente", lambda _id: asyncio.sleep(0, {"total": Decimal("380"), "detalle": []}))

    resultado = asyncio.run(servicio.enviar_recordatorios_promesa(hoy=date(2026, 10, 6)))

    assert resultado == {"recordatorios_promesa": 1}
    aviso = enviados[0]
    assert aviso["tipo_evento"] == "recordatorio_promesa" and aviso["cliente_id"] == 5
    assert aviso["variables_extra"]["monto_promesa"] == "$380.00"
    assert aviso["clave_dedupe"] == "factura:10:promesa:2026-10-06"
    assert "facturas.fecha_promesa_pago =" in servicio.db.consultas[0]


def test_hay_mensaje_aunque_no_se_haya_creado_la_plantilla():
    assert "{monto_promesa}" in PLANTILLAS_OBLIGATORIAS["recordatorio_promesa"]
