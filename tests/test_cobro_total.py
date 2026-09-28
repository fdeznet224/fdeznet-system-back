import asyncio
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.application.services.billing_service import BillingService
from src.infrastructure.models import FacturaConceptoModel


class _Resultado:
    def __init__(self, valores):
        self.valores = valores

    def scalars(self):
        return self

    def all(self):
        return self.valores


class _DB:
    def __init__(self, objetos=None, pagos_previos=None):
        self.objetos = objetos or {}
        self.pagos_previos = pagos_previos or []
        self.agregados = []
        self.commits = 0

    async def execute(self, _statement):
        return _Resultado(self.pagos_previos)

    async def get(self, _model, id_):
        return self.objetos.get(id_)

    def add(self, value):
        self.agregados.append(value)

    async def commit(self):
        self.commits += 1


def _preparar(monkeypatch, saldos, pagos_previos=None):
    cliente = SimpleNamespace(id=20, telefono=None, saldo_a_favor=Decimal("0"))
    db = _DB({20: cliente}, pagos_previos)
    service = BillingService(db)
    llamadas = []

    async def estado(_cliente_id):
        return {
            "facturas": [
                {"id": i + 1, "saldo_pendiente": Decimal(s)}
                for i, s in enumerate(saldos)
            ]
        }

    async def pagar(**kwargs):
        llamadas.append((kwargs["factura_id"], kwargs["monto"]))
        assert kwargs["confirmar_transaccion"] is False
        assert kwargs["enviar_notificacion"] is False
        return {"pago_id": 100 + kwargs["factura_id"], "reactivado": False}

    async def pendientes(_cliente_id):
        return []

    async def notificar(*_args):
        return None

    monkeypatch.setattr(service, "estado_cuenta_cliente", estado)
    monkeypatch.setattr(service, "registrar_pago_completo", pagar)
    monkeypatch.setattr(service, "_listar_facturas_pendientes_cliente", pendientes)
    monkeypatch.setattr(service, "_notificar_cobro_total", notificar)
    return service, llamadas, db


@pytest.mark.parametrize(
    "recibido, esperado",
    [
        # Pago exacto: prorrateo + mensualidad con reconexión.
        ("470.00", [(1, Decimal("90.00")), (2, Decimal("380.00"))]),
        # Abono: cubre lo más antiguo y abona a lo siguiente.
        ("200.00", [(1, Decimal("90.00")), (2, Decimal("110.00"))]),
        # No alcanza ni lo más antiguo.
        ("50.00", [(1, Decimal("50.00"))]),
        # Excedente: la última factura recibe el resto (saldo a favor).
        ("500.00", [(1, Decimal("90.00")), (2, Decimal("410.00"))]),
    ],
)
def test_cobro_total_se_aplica_de_lo_mas_antiguo(monkeypatch, recibido, esperado):
    service, llamadas, db = _preparar(monkeypatch, ["90.00", "380.00"])

    resultado = asyncio.run(
        service.registrar_pago_cliente(20, SimpleNamespace(id=1), "efectivo", recibido)
    )

    assert llamadas == esperado
    assert resultado["total_recibido"] == Decimal(recibido)
    assert db.commits == 1


def test_cobro_total_repetido_no_se_aplica_dos_veces(monkeypatch):
    previo = SimpleNamespace(id=7, monto_total=Decimal("470.00"))
    service, llamadas, _ = _preparar(
        monkeypatch, ["90.00", "380.00"], pagos_previos=[previo]
    )

    resultado = asyncio.run(
        service.registrar_pago_cliente(
            20, SimpleNamespace(id=1), "efectivo", "470.00",
            clave_idempotencia="op-1",
        )
    )

    assert resultado["idempotente"] is True
    assert llamadas == []


def test_cobro_total_sin_deuda_se_rechaza(monkeypatch):
    service, _, _ = _preparar(monkeypatch, [])

    with pytest.raises(ValueError, match="saldo pendiente"):
        asyncio.run(
            service.registrar_pago_cliente(20, SimpleNamespace(id=1), "efectivo", "10")
        )


def _factura():
    return SimpleNamespace(
        id=939,
        monto=Decimal("350.00"),
        total=Decimal("350.00"),
        saldo_pendiente=Decimal("350.00"),
        cargos_adicionales_total=Decimal("0.00"),
        detalles=None,
    )


def test_reconexion_se_suma_a_la_factura_del_corte():
    plantilla = SimpleNamespace(cargo_reconexion=Decimal("30.00"))
    db = _DB({5: plantilla})
    factura = _factura()
    cliente = SimpleNamespace(id=20, plantilla_id=5)

    monto = asyncio.run(
        BillingService(db)._agregar_reconexion_a_factura(factura, cliente, None)
    )

    assert monto == Decimal("30.00")
    assert factura.total == Decimal("380.00")
    assert factura.saldo_pendiente == Decimal("380.00")
    assert factura.cargos_adicionales_total == Decimal("30.00")
    concepto = next(a for a in db.agregados if isinstance(a, FacturaConceptoModel))
    assert concepto.factura_id == 939
    assert concepto.tipo == "reconexion"
    assert concepto.afecta_corte is True
    assert concepto.saldo_pendiente == Decimal("30.00")


def test_reconexion_desactivada_con_cargo_cero():
    plantilla = SimpleNamespace(cargo_reconexion=Decimal("0"))
    db = _DB({5: plantilla})
    factura = _factura()

    monto = asyncio.run(
        BillingService(db)._agregar_reconexion_a_factura(
            factura, SimpleNamespace(id=20, plantilla_id=5), None
        )
    )

    assert monto == Decimal("0.00")
    assert factura.total == Decimal("350.00")
    assert db.agregados == []


def _factura_texto(**datos):
    base = {
        "id": 598,
        "es_prorrateada": False,
        "tipo_factura": "mensual",
        "mes_correspondiente": None,
        "concepto": None,
        "periodo_desde": date(2026, 7, 15),
        "periodo_hasta": date(2026, 8, 14),
        "fecha_vencimiento": date(2026, 7, 15),
    }
    return SimpleNamespace(**{**base, **datos})


def test_factura_vieja_sin_renglones_dice_el_mes_no_el_folio():
    textos = BillingService._textos_factura(
        _factura_texto(saldo_pendiente=Decimal("261.29")), []
    )

    assert textos == [("Mensualidad de julio 2026", True, Decimal("261.29"))]


def test_textos_de_renglones_en_palabras():
    conceptos = [
        SimpleNamespace(tipo="internet", concepto="Servicio de internet", saldo_pendiente=Decimal("300")),
        SimpleNamespace(tipo="servicio_adicional", concepto="IPTV", saldo_pendiente=Decimal("50")),
        SimpleNamespace(tipo="reconexion", concepto="Cargo por reconexión", saldo_pendiente=Decimal("30")),
        SimpleNamespace(tipo="internet_prorrateado", concepto="Prorrateo", saldo_pendiente=Decimal("120")),
    ]

    textos = BillingService._textos_factura(
        _factura_texto(periodo_desde=date(2026, 10, 15)), conceptos
    )

    assert textos == [
        ("Mensualidad de octubre 2026", True, Decimal("300")),
        ("Servicio extra: IPTV", False, Decimal("50")),
        ("Cargo por reconexión", False, Decimal("30")),
        ("Prorrateo de instalación", False, Decimal("120")),
    ]


def test_prorrateo_muestra_sus_fechas():
    factura = _factura_texto(
        es_prorrateada=True,
        tipo_factura="prorrateo",
        periodo_desde=date(2026, 9, 3),
        periodo_hasta=date(2026, 9, 14),
        saldo_pendiente=Decimal("140.00"),
    )

    assert BillingService._textos_factura(factura, []) == [
        ("Prorrateo del 03/09 al 14/09/2026", True, Decimal("140.00"))
    ]


def test_sincronizacion_offline_acepta_pago_de_cliente(monkeypatch):
    from src.application.services import sync_service as modulo

    llamadas = {}

    class Billing:
        def __init__(self, _db):
            pass

        async def registrar_pago_cliente(self, **kwargs):
            llamadas.update(kwargs)
            return {"status": "ok"}

    monkeypatch.setattr(modulo, "BillingService", Billing)
    servicio = modulo.SyncService(db=None)

    respuesta = asyncio.run(
        servicio._registrar_pago_cliente(
            "op-9",
            {"cliente_id": 20, "metodo_pago": "efectivo", "monto_recibido": "470.00"},
            SimpleNamespace(rol="cajero"),
        )
    )

    assert respuesta == {"status": "ok"}
    assert llamadas["cliente_id"] == 20
    assert llamadas["monto"] == Decimal("470.00")
    assert llamadas["clave_idempotencia"] == "op-9"
    assert "pago_cliente" in modulo.TIPOS_SINCRONIZABLES


def test_mes_actual_es_la_mensualidad_mas_reciente():
    agosto = _factura_texto(id=1, periodo_desde=date(2026, 8, 15))
    septiembre = _factura_texto(id=2, periodo_desde=date(2026, 9, 15))
    octubre = _factura_texto(id=3, periodo_desde=date(2026, 10, 15))
    prorrateo = _factura_texto(id=4, es_prorrateada=True, tipo_factura="prorrateo")

    assert BillingService._mensualidad_actual(
        [agosto, septiembre, octubre, prorrateo]
    ) is octubre
    assert BillingService._mensualidad_actual([octubre]) is octubre
    assert BillingService._mensualidad_actual([prorrateo]) is None


def test_mensaje_de_factura_lleva_total_y_desglose_si_hay_atrasos():
    estado = {
        "total": Decimal("611.29"),
        "detalle": [
            {"texto": "Mensualidad de agosto 2026", "monto": Decimal("261.29"), "actual": False},
            {"texto": "Mensualidad de octubre 2026", "monto": Decimal("300.00"), "actual": True},
            {"texto": "Servicio extra: IPTV", "monto": Decimal("50.00"), "actual": False},
        ],
    }

    variables = BillingService._variables_total_a_pagar(estado)

    assert variables["total_a_pagar"] == "$611.29"
    assert variables["desglose_total"].splitlines() == [
        "📋 *Total a pagar: $611.29*",
        "• Mensualidad de agosto 2026: $261.29",
        "• Mensualidad de octubre 2026 (este mes): $300.00",
        "• Servicio extra: IPTV: $50.00",
    ]


def test_sin_atrasos_ni_extras_no_repite_el_monto():
    estado = {
        "total": Decimal("300.00"),
        "detalle": [
            {"texto": "Mensualidad de octubre 2026", "monto": Decimal("300.00"), "actual": True},
        ],
    }

    variables = BillingService._variables_total_a_pagar(estado)

    assert variables == {"total_a_pagar": "$300.00", "desglose_total": ""}


def test_recordatorio_y_avisos_de_corte_llevan_el_total():
    from src.application.services.notification_service import EVENTOS_CON_TOTAL

    assert {
        "nueva_factura",
        "recordatorio_pago",
        "corte_ejecutado",
        "aviso_corte",
        "corte_servicio",
    } <= EVENTOS_CON_TOTAL
