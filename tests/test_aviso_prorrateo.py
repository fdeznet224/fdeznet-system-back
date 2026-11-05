"""El prorrateo se le avisa al cliente: no corta, pero se suma a su próxima factura."""
import asyncio
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from src.application.services import billing_service
from src.application.services.billing_service import BillingService
from src.application.services.notification_service import PLANTILLAS_OBLIGATORIAS


def _factura(**cambios):
    datos = dict(
        id=77, cliente_id=5, tipo_factura="prorrateo", total=Decimal("120.00"), saldo_pendiente=Decimal("120.00"),
        periodo_desde=date(2026, 11, 3), periodo_hasta=date(2026, 11, 14), fecha_vencimiento=date(2026, 11, 15),
        dias_con_servicio=12, dias_sin_servicio=0, monto_servicio_original=Decimal("120.00"),
        ajuste_suspension=0, cargos_adicionales_total=0, detalles=None, descripcion="Del 3 al 14 de noviembre",
    )
    datos.update(cambios)
    return SimpleNamespace(**datos)


def test_avisa_el_prorrateo_con_su_proxima_factura(monkeypatch):
    enviados = []

    class _Notificador:
        def __init__(self, _db):
            pass

        async def notificar(self, tipo, cliente_id, variables_extra=None, clave_dedupe=None):
            enviados.append((tipo, cliente_id, variables_extra, clave_dedupe))
            return True

    monkeypatch.setattr(billing_service, "NotificationService", _Notificador)
    assert asyncio.run(BillingService(None).avisar_prorrateo(_factura())) is True
    [(tipo, cliente, variables, clave)] = enviados
    assert (tipo, cliente, clave) == ("aviso_prorrateo", 5, "factura:77:prorrateo")
    assert variables["total_factura"] == "$120.00" and variables["fecha_proxima_factura"] == "15/11/2026"
    assert variables["periodo_desde"] == "03/11/2026" and variables["dias_con_servicio"] == "12"


def test_no_avisa_si_ya_esta_pagado_o_no_es_prorrateo(monkeypatch):
    monkeypatch.setattr(billing_service, "NotificationService", lambda _db: (_ for _ in ()).throw(AssertionError()))
    servicio = BillingService(None)
    assert asyncio.run(servicio.avisar_prorrateo(None)) is False
    assert asyncio.run(servicio.avisar_prorrateo(_factura(saldo_pendiente=Decimal("0")))) is False
    assert asyncio.run(servicio.avisar_prorrateo(_factura(tipo_factura="mensual"))) is False


def test_la_plantilla_dice_que_no_corta_y_cuando_se_cobra():
    texto = PLANTILLAS_OBLIGATORIAS["aviso_prorrateo"]
    assert "no corta tu servicio" in texto and "{fecha_proxima_factura}" in texto and "{total_factura}" in texto


class _Resultado:
    def __init__(self, filas):
        self.filas = filas

    def scalars(self):
        return self

    def unique(self):
        return self

    def all(self):
        return self.filas


class _Db:
    def __init__(self, filas):
        self.filas, self.consultas, self.commits = filas, [], 0

    async def execute(self, consulta):
        self.consultas.append(str(consulta.compile(compile_kwargs={"literal_binds": True})))
        return _Resultado(self.filas)

    async def commit(self):
        self.commits += 1


def test_recordatorio_un_dia_antes_de_sumarse_a_la_factura(monkeypatch):
    enviados = []

    class _Notificador:
        def __init__(self, _db):
            pass

        async def notificar(self, tipo, cliente_id, variables_extra=None, clave_dedupe=None):
            enviados.append((tipo, clave_dedupe, variables_extra["fecha_proxima_factura"]))
            return True

    monkeypatch.setattr(billing_service, "NotificationService", _Notificador)
    con_tel = _factura(cliente=SimpleNamespace(telefono="9611112233"))
    sin_tel = _factura(id=78, cliente=SimpleNamespace(telefono=None))
    db = _Db([con_tel, sin_tel])
    resultado = asyncio.run(BillingService(db).enviar_recordatorios_prorrateo(date(2026, 11, 14)))
    assert resultado == {"recordatorios_prorrateo": 1}
    assert enviados == [("recordatorio_prorrateo", "factura:77:prorrateo-recordatorio", "15/11/2026")]
    assert "'2026-11-15'" in db.consultas[0] and "prorrateo" in db.consultas[0]  # vence mañana


def test_el_tecnico_cobra_en_efectivo_o_queda_para_despues(monkeypatch):
    from src.application.services import activacion_service
    from src.application.services.activacion_service import ActivacionService

    llamadas = []
    factura = _factura(saldo_pendiente=Decimal("120.00"))

    class _Billing:
        def __init__(self, _db):
            pass

        async def registrar_pago_completo(self, **datos):
            llamadas.append(("pago", datos["metodo_pago"], datos["monto"], datos["usuario_operador"].id))
            factura.saldo_pendiente = Decimal("0")

        async def avisar_prorrateo(self, f):
            llamadas.append(("aviso", f.id))

    class _DbVacia:
        async def commit(self):
            pass

    servicio = ActivacionService(_DbVacia())

    async def factura_de_orden(_orden, _usuario):
        return factura

    async def estado(_orden, _usuario):
        return {"pagado": factura.saldo_pendiente <= 0}

    monkeypatch.setattr(servicio, "_factura_prorrateo", factura_de_orden)
    monkeypatch.setattr(servicio, "prorrateo", estado)
    monkeypatch.setattr("src.application.services.billing_service.BillingService", _Billing)
    tecnico = SimpleNamespace(id=9)

    assert asyncio.run(servicio.decidir_prorrateo(5, "despues", tecnico)) == {"pagado": False}
    assert asyncio.run(servicio.decidir_prorrateo(5, "efectivo", tecnico)) == {"pagado": True}
    assert llamadas == [("aviso", 77), ("pago", "efectivo", Decimal("120.00"), 9)]
    # Ya pagado no se cobra otra vez.
    asyncio.run(servicio.decidir_prorrateo(5, "efectivo", tecnico))
    assert len(llamadas) == 2
    assert activacion_service  # módulo importado
