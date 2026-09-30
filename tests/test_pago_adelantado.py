"""Pagos del mes siguiente: el banco los confirma, pero se aplican el día 1."""

import asyncio
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

import src.application.services.bank_email_service as banco_mod
from src.application.services.bank_email_service import BankEmailService, aplicar_pago_desde

CONFIG = SimpleNamespace(
    activo=True, auto_aprobar=True, cuentas_destino_permitidas="6342,5265",
    tolerancia_monto=Decimal("0"), ventana_dias=3,
)


def _factura(**cambios):
    datos = dict(
        id=1134, cliente_id=248, saldo_pendiente=Decimal("300.00"),
        periodo_desde=date(2026, 10, 1), fecha_vencimiento=date(2026, 10, 1),
    )
    datos.update(cambios)
    return SimpleNamespace(**datos)


MARGARITA = SimpleNamespace(id=248, nombre="Margarita Moreno Lopez", cedula="D440")
CONCEPTO_MARGARITA = "margarita moreno lopez De la cuenta: BANORTE ***6634"
CONCEPTO_MAURICIO = "fdeznet2E3A Folio: M01576668"


def _escenario(estado_deposito="disponible", concepto=CONCEPTO_MARGARITA):
    correo = SimpleNamespace(
        id=89, autenticado=True, estado=estado_deposito, tipo_movimiento="entrante",
        cuenta_destino_terminacion="5265", monto=Decimal("300.00"),
        fecha_correo=datetime(2026, 9, 30, 14, 54, 59), referencia="M01576668",
        concepto=concepto,
    )
    revision = SimpleNamespace(
        id=13, estado="pendiente", cliente_id=248, factura_id=1134, transaccion_correo_id=89,
        folio_detectado=None, monto_detectado=Decimal("300.00"),
        fecha_pago_detectada=datetime(2026, 9, 30, 8, 58, 12),
        fecha_recepcion=datetime(2026, 9, 30, 9, 2, 31),
        motivo_revision=None, notas_revision=None,
    )
    return correo, revision


class _DB:
    def __init__(self, objetos):
        self.objetos = objetos

    async def get(self, modelo, llave):
        return self.objetos.get((modelo.__name__, llave))

    async def commit(self):
        return None


def _conciliar(
    monkeypatch, ahora, factura=None, otros_depositos=(), estado_deposito="disponible",
    concepto=CONCEPTO_MARGARITA,
):
    correo, revision = _escenario(estado_deposito, concepto)
    db = _DB({
        ("ClienteModel", 248): MARGARITA,
        ("TransaccionCorreoBancoModel", 89): correo,
        ("FacturaModel", 1134): factura or _factura(),
    })

    class _Reloj(datetime):
        @classmethod
        def now(cls, tz=None):
            return ahora

    monkeypatch.setattr(banco_mod, "datetime", _Reloj)
    servicio = BankEmailService()
    monkeypatch.setattr(servicio, "get_config", lambda _db: asyncio.sleep(0, CONFIG))
    monkeypatch.setattr(
        servicio, "_candidatos_sin_referencia",
        lambda *_a: asyncio.sleep(0, [correo, *otros_depositos]),
    )
    llego_a_cobrar = []

    async def _operador(*_a, **_k):
        llego_a_cobrar.append(True)
        raise RuntimeError("cobro")  # basta con saber que pasó la regla del día 1

    monkeypatch.setattr(db, "execute", _operador, raising=False)
    try:
        resultado = asyncio.run(servicio.reconcile_revision(db, revision))
    except RuntimeError:
        resultado = None
    return resultado, correo, revision, bool(llego_a_cobrar)


def test_el_pago_del_mes_siguiente_se_aparta_para_el_dia_1(monkeypatch):
    resultado, correo, revision, cobro = _conciliar(monkeypatch, datetime(2026, 9, 30, 9, 30))
    assert resultado == {
        "status": "pago_adelantado", "approved": False,
        "transaction_id": 89, "aplicar_el": "2026-10-01",
    }
    assert correo.estado == "reservada"
    assert revision.motivo_revision == "pago_adelantado"
    assert revision.notas_revision == "Se aplica el 01/10/2026"
    assert not cobro


def test_el_dia_1_antes_de_las_8_todavia_espera(monkeypatch):
    resultado, *_ , cobro = _conciliar(monkeypatch, datetime(2026, 10, 1, 7, 59))
    assert resultado["status"] == "pago_adelantado" and not cobro


def test_el_dia_1_a_las_8_se_aplica_con_el_deposito_apartado(monkeypatch):
    correo, revision = _escenario()
    correo.estado = "reservada"
    assert BankEmailService.motivo_coincidencia(CONFIG, revision, correo) == "coincidencia_sin_referencia"
    *_, cobro = _conciliar(monkeypatch, datetime(2026, 10, 1, 8, 0))
    assert cobro


def test_un_deposito_apartado_no_lo_toma_otro_comprobante():
    correo, revision = _escenario()
    correo.estado = "reservada"
    revision.transaccion_correo_id = 90
    assert BankEmailService.motivo_coincidencia(CONFIG, revision, correo) == "correo_bancario_ya_utilizado"


def test_el_pago_del_mes_en_curso_se_aplica_de_inmediato(monkeypatch):
    septiembre = _factura(periodo_desde=date(2026, 9, 1), fecha_vencimiento=date(2026, 10, 5))
    *_, cobro = _conciliar(monkeypatch, datetime(2026, 9, 30, 9, 30), septiembre)
    assert cobro


@pytest.mark.parametrize(
    "periodo, vence, esperado",
    [
        (date(2026, 10, 15), date(2026, 10, 15), datetime(2026, 10, 1, 8, 0)),  # día de instalación
        (None, date(2026, 11, 1), datetime(2026, 11, 1, 8, 0)),
        (None, None, None),
    ],
)
def test_se_aplica_el_dia_1_del_mes_que_cubre(periodo, vence, esperado):
    assert aplicar_pago_desde(_factura(periodo_desde=periodo, fecha_vencimiento=vence)) == esperado


def test_quien_ya_pago_por_adelantado_no_recibe_recordatorio():
    import inspect
    from src.application.services.billing_service import BillingService

    fuente = inspect.getsource(BillingService.enviar_recordatorios_automaticos)
    assert '"pago_adelantado"' in fuente and "~exists()" in fuente


def _deposito(id, concepto):
    return SimpleNamespace(id=id, concepto=concepto)


def test_caso_de_hoy_el_deposito_ligado_era_de_otro_cliente(monkeypatch):
    # Margarita quedó ligada al #89 (concepto de Mauricio) y el #90 lleva su nombre.
    resultado, correo, revision, cobro = _conciliar(
        monkeypatch, datetime(2026, 10, 1, 8, 0), estado_deposito="reservada",
        concepto=CONCEPTO_MAURICIO, otros_depositos=[_deposito(90, "margarita moreno lopez")],
    )
    assert resultado == {"status": "multiples_correos_coincidentes", "approved": False}
    assert not cobro
    assert revision.transaccion_correo_id is None
    assert revision.notas_revision == "El depósito #89 no es del cliente; el que coincide es #90"
    assert correo.estado == "disponible"


def test_si_otro_deposito_igual_no_es_del_cliente_se_cobra_el_suyo(monkeypatch):
    *_, cobro = _conciliar(
        monkeypatch, datetime(2026, 10, 1, 8, 0), estado_deposito="reservada",
        otros_depositos=[_deposito(90, CONCEPTO_MAURICIO)],
    )
    assert cobro


def test_si_dos_depositos_llevan_su_nombre_decide_una_persona(monkeypatch):
    resultado, correo, _, cobro = _conciliar(
        monkeypatch, datetime(2026, 10, 1, 8, 0), estado_deposito="reservada",
        otros_depositos=[_deposito(90, "MARGARITA MORENO pago")],
    )
    assert resultado["status"] == "multiples_correos_coincidentes" and not cobro
    assert correo.estado == "disponible"


def test_sin_nada_del_cliente_en_el_deposito_no_se_cobra(monkeypatch):
    resultado, _, revision, cobro = _conciliar(
        monkeypatch, datetime(2026, 10, 1, 8, 0), estado_deposito="reservada", concepto="Envio Folio: 000001971",
    )
    assert resultado["status"] == "titular_no_coincide" and not cobro
    assert revision.notas_revision.startswith("El depósito #89 no se confirmó como del cliente")
