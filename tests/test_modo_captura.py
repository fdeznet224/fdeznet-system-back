"""Modo "solo captura": el pago se confirma con los datos de la captura."""

import asyncio
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

import src.application.services.comprobante_service as comprobante_mod
import src.jobs as jobs
from src.application.services.comprobante_service import ComprobanteService, clave_de_transferencia
from src.application.services.ocr_service import OCRService

AHORA = datetime.now().replace(microsecond=0)
CONFIG = SimpleNamespace(validar_pagos_con="captura", ventana_dias=3, cuentas_destino_permitidas="6342,5265", activo=False)
CLIENTE = SimpleNamespace(id=248, nombre="Margarita Moreno Lopez", cedula="D440", estado="activo")
ADMIN = SimpleNamespace(id=1, rol="admin")


def _factura(**cambios):
    hoy = date.today()
    datos = dict(id=1134, cliente_id=248, saldo_pendiente=Decimal("300.00"),
                 periodo_desde=hoy.replace(day=1), fecha_vencimiento=hoy)
    datos.update(cambios)
    return SimpleNamespace(**datos)


def _revision(**cambios):
    datos = dict(
        id=13, estado="pendiente", cliente_id=None, factura_id=None, pago_id=None,
        telefono="95133743751296@lid", folio_detectado=None, monto_detectado=Decimal("300.00"),
        fecha_pago_detectada=AHORA - timedelta(minutes=5), fecha_recepcion=AHORA,
        huella_captura="SC-0123456789ABCDEF", motivo_revision="sin_referencia", notas_revision=None,
        transaccion_correo_id=None, concepto_detectado="margarita moreno lopez", auditoria_banco=None,
        fecha_revision=None,
    )
    datos.update(cambios)
    return SimpleNamespace(**datos)


class _Primero:
    def __init__(self, valor):
        self.valor = valor

    def scalars(self):
        return self

    def first(self):
        return self.valor


class _DB:
    def __init__(self, revision, clave_usada=False, conteos=None):
        self.revision = revision
        self.clave_usada = clave_usada
        self.agregados = []
        # Respuestas en orden: folio usado, otra revisión, pagos del mes, capturas de hoy.
        self.respuestas = list(conteos or [])

    async def scalar(self, _consulta):
        if self.clave_usada:
            return 99
        return self.respuestas.pop(0) if self.respuestas else None

    async def execute(self, _consulta):
        return _Primero(ADMIN)

    async def get(self, _modelo, _llave):
        return self.revision

    def add(self, objeto):
        self.agregados.append(objeto)

    async def commit(self):
        return None

    async def rollback(self):
        return None


def _aplicar(monkeypatch, revision=None, factura=None, clave_usada=False, idempotente=False, conteos=None, cliente=None):
    cobros, alertas = [], []

    class _Billing:
        def __init__(self, _db):
            pass

        async def registrar_pago_completo(self, **kwargs):
            cobros.append(kwargs)
            return {"pago_id": 700, "idempotente": idempotente}

    monkeypatch.setattr(comprobante_mod, "BillingService", _Billing)
    revision = revision or _revision()
    db = _DB(revision, clave_usada, conteos)

    async def avisar(numero, texto):
        alertas.append(texto)

    servicio = ComprobanteService(db, correo=SimpleNamespace(), avisar_admin=avisar)

    async def config_alertas(*_a):
        return SimpleNamespace(telefonos_alerta="9611111111")

    monkeypatch.setattr(db, "get", lambda modelo, llave: (
        config_alertas() if modelo.__name__ == "ConfiguracionSistema" else asyncio.sleep(0, revision)
    ))
    resultado = asyncio.run(servicio.aplicar_por_captura(revision, cliente or CLIENTE, factura or _factura(), CONFIG))
    return resultado, revision, db, cobros, alertas


def test_la_misma_captura_da_el_mismo_codigo_aunque_la_reclame_otro_contrato():
    captura = ("Transferencia exitosa $300.00 Cuenta ****8663 Concepto: margarita moreno lopez "
               "30/09/2026 08.58.12")
    para_ella = OCRService.extraer_datos(captura)
    para_otro = OCRService.extraer_datos(captura.replace("margarita moreno lopez", "contrato 2E3A"))
    assert para_ella["huella"] == para_otro["huella"]


def test_el_codigo_es_el_folio_si_la_captura_lo_trae():
    assert clave_de_transferencia(_revision(folio_detectado="M0157666B")) == "M0157666B"
    assert clave_de_transferencia(_revision(folio_detectado="MO1576668")) == "M01576668"  # O por cero
    assert clave_de_transferencia(_revision()) == "SC-0123456789ABCDEF"


def test_una_captura_valida_se_aplica_sin_esperar_al_banco(monkeypatch):
    resultado, revision, db, cobros, _ = _aplicar(monkeypatch)
    assert resultado["aplicado"] is True and resultado["estado"] == "pago_confirmado_por_captura"
    assert cobros[0]["clave_idempotencia"] == "captura:SC-0123456789ABCDEF"
    assert cobros[0]["monto"] == Decimal("300.00")
    assert revision.estado == "aprobado" and revision.motivo_revision == "aprobado_por_captura"
    assert db.agregados[0].folio_banco == "SC-0123456789ABCDEF"  # ya no se puede volver a usar


def test_si_el_codigo_ya_se_uso_rebota_y_se_avisa(monkeypatch):
    resultado, revision, _, cobros, alertas = _aplicar(monkeypatch, clave_usada=True)
    assert resultado["estado"] == "duplicado" and not cobros
    assert revision.estado == "rechazado" and revision.motivo_revision == "captura_ya_utilizada"
    assert "POSIBLE FRAUDE" in alertas[0]


def test_dos_mensajes_a_la_vez_con_la_misma_captura_no_cobran_dos_veces(monkeypatch):
    resultado, revision, _, _, _ = _aplicar(monkeypatch, idempotente=True)
    assert resultado["estado"] == "duplicado" and revision.estado == "rechazado"


def test_el_monto_de_la_captura_debe_cubrir_la_deuda(monkeypatch):
    resultado, revision, _, cobros, _ = _aplicar(monkeypatch, revision=_revision(monto_detectado=Decimal("30.00")))
    assert resultado["estado"] == "monto_menor_a_la_deuda" and not cobros
    assert revision.estado == "pendiente"


def test_sin_folio_ni_hora_no_hay_codigo_y_se_pide_el_comprobante(monkeypatch):
    resultado, *_ , cobros, _ = _aplicar(monkeypatch, revision=_revision(huella_captura=None, fecha_pago_detectada=None))
    assert resultado["estado"] == "captura_incompleta" and not cobros


@pytest.mark.parametrize("desfase, motivo", [(timedelta(days=-5), "captura_antigua"),
                                             (timedelta(hours=2), "fecha_de_captura_invalida")])
def test_la_fecha_de_la_captura_debe_ser_reciente(monkeypatch, desfase, motivo):
    revision = _revision(fecha_pago_detectada=AHORA + desfase)
    resultado, revision, _, cobros, _ = _aplicar(monkeypatch, revision=revision)
    assert resultado["estado"] == motivo and not cobros and revision.estado == "pendiente"


def test_el_pago_del_mes_siguiente_espera_al_dia_1(monkeypatch):
    siguiente = (date.today().replace(day=1) + timedelta(days=32)).replace(day=1)
    resultado, revision, _, cobros, _ = _aplicar(monkeypatch, factura=_factura(periodo_desde=siguiente))
    assert resultado["estado"] == "pago_adelantado" and not cobros
    assert revision.motivo_revision == "pago_adelantado" and revision.cliente_id == 248


def test_en_modo_captura_no_se_concilia_con_el_correo(monkeypatch):
    llamadas = []

    class _Banco:
        async def get_config(self, _db):
            return CONFIG

        async def sync(self, _db):
            llamadas.append("sync")

    class _Comprobantes:
        def __init__(self, _db):
            pass

        async def aplicar_adelantados_por_captura(self):
            llamadas.append("adelantados")

    class _Sesion:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_a):
            return None

    monkeypatch.setattr(jobs, "BankEmailService", _Banco)
    monkeypatch.setattr(jobs, "ComprobanteService", _Comprobantes)
    monkeypatch.setattr(jobs, "SessionLocal", _Sesion)
    asyncio.run(jobs.tarea_conciliar_correos_bancarios())
    assert llamadas == ["adelantados"]



# ------------------------------------------------------------- límites
def test_mas_del_doble_de_la_deuda_lo_revisa_una_persona(monkeypatch):
    resultado, revision, _, cobros, _ = _aplicar(monkeypatch, revision=_revision(monto_detectado=Decimal("900.00")))
    assert resultado["estado"] == "monto_mayor_al_normal" and not cobros and revision.estado == "pendiente"


def test_dos_meses_juntos_si_se_aplican(monkeypatch):
    resultado, *_ = _aplicar(monkeypatch, revision=_revision(monto_detectado=Decimal("600.00")))
    assert resultado["aplicado"] is True


def test_el_segundo_pago_por_captura_del_mes_lo_revisa_una_persona(monkeypatch):
    resultado, _, _, cobros, _ = _aplicar(monkeypatch, conteos=[None, None, 1, 0])
    assert resultado["estado"] == "segundo_pago_del_mes" and not cobros


def test_muchas_capturas_del_mismo_chat_en_un_dia(monkeypatch):
    resultado, _, _, cobros, _ = _aplicar(monkeypatch, conteos=[None, None, 0, 2])
    assert resultado["estado"] == "muchas_capturas_hoy" and not cobros


def test_queda_pendiente_de_auditar_y_con_prioridad_si_reconecta(monkeypatch):
    _, revision, *_ = _aplicar(monkeypatch)
    assert revision.auditoria_banco == "pendiente"
    cortado = SimpleNamespace(**{**vars(CLIENTE), "estado": "suspendido"})
    _, revision, *_ = _aplicar(monkeypatch, cliente=cortado)
    assert revision.auditoria_banco == "prioridad"


def test_avisa_si_el_concepto_no_traia_el_contrato(monkeypatch):
    resultado, *_ = _aplicar(monkeypatch)
    assert resultado["concepto_traia_contrato"] is False
    resultado, *_ = _aplicar(monkeypatch, revision=_revision(concepto_detectado="pago D440"))
    assert resultado["concepto_traia_contrato"] is True


# ------------------------------------------- auditoría con el correo del banco
from src.application.services.bank_email_service import BankEmailService  # noqa: E402


class _DBAuditoria:
    def __init__(self, revisiones):
        self.revisiones = revisiones

    async def execute(self, _consulta):
        revisiones = self.revisiones

        class _R:
            def scalars(self):
                return self

            def all(self):
                return revisiones

        return _R()

    async def get(self, *_a):
        return CLIENTE

    async def commit(self):
        return None


def _auditar(monkeypatch, revisiones, encontrado=None, candidatos=()):
    servicio = BankEmailService()
    monkeypatch.setattr(servicio, "get_config", lambda _db: asyncio.sleep(0, CONFIG))
    monkeypatch.setattr(servicio, "find_match", lambda _db, _r: asyncio.sleep(0, (encontrado, "x")))
    monkeypatch.setattr(servicio, "_candidatos_sin_referencia", lambda *_a: asyncio.sleep(0, list(candidatos)))
    return asyncio.run(servicio.auditar_pagos_por_captura(_DBAuditoria(revisiones)))


def _aprobada(**cambios):
    return _revision(**{"estado": "aprobado", "pago_id": 700, "auditoria_banco": "pendiente",
                        "fecha_revision": datetime.now() - timedelta(hours=1), "cliente_id": 248, **cambios})


def test_si_llega_el_deposito_se_liga_y_queda_confirmado(monkeypatch):
    deposito = SimpleNamespace(id=82, estado="disponible", pago_id=None, conciliada_en=None)
    revision = _aprobada(folio_detectado="2026093040014BMOVP020422198180")
    assert _auditar(monkeypatch, [revision], encontrado=deposito) == []
    assert revision.auditoria_banco == "confirmado" and revision.transaccion_correo_id == 82
    assert deposito.estado == "conciliada" and deposito.pago_id == 700


def test_sin_folio_basta_un_unico_deposito_del_mismo_monto_y_hora(monkeypatch):
    deposito = SimpleNamespace(id=90, estado="disponible", pago_id=None, conciliada_en=None)
    revision = _aprobada()
    _auditar(monkeypatch, [revision], candidatos=[deposito])
    assert revision.auditoria_banco == "confirmado"


def test_sin_deposito_en_24_horas_se_avisa(monkeypatch):
    reciente = _aprobada(id=1)
    vieja = _aprobada(id=2, fecha_revision=datetime.now() - timedelta(hours=25))
    reconexion = _aprobada(id=3, auditoria_banco="prioridad", fecha_revision=datetime.now() - timedelta(hours=3))
    avisos = _auditar(monkeypatch, [reciente, vieja, reconexion])
    assert [a["id"] for a in avisos] == [2, 3] and avisos[1]["reconexion"] is True
    assert reciente.auditoria_banco == "pendiente" and vieja.auditoria_banco == "sin_deposito"


def test_en_modo_captura_con_correo_se_audita(monkeypatch):
    llamadas = []
    config = SimpleNamespace(**{**vars(CONFIG), "activo": True})

    class _Banco:
        async def get_config(self, _db):
            return config

        async def sync(self, _db):
            llamadas.append("sync")

        async def auditar_pagos_por_captura(self, _db):
            llamadas.append("auditar")
            return [{"id": 2, "cliente": "X", "monto": 300, "folio": "SC-1", "reconexion": False}]

    class _Comprobantes:
        def __init__(self, _db):
            pass

        async def aplicar_adelantados_por_captura(self):
            llamadas.append("adelantados")

    class _Sesion:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_a):
            return None

    async def alerta(mensaje, _db, tipo_evento="alerta_router"):
        llamadas.append("alerta")

    monkeypatch.setattr(jobs, "BankEmailService", _Banco)
    monkeypatch.setattr(jobs, "ComprobanteService", _Comprobantes)
    monkeypatch.setattr(jobs, "SessionLocal", _Sesion)
    monkeypatch.setattr(jobs, "enviar_alertas_whatsapp", alerta)
    asyncio.run(jobs.tarea_conciliar_correos_bancarios())
    assert llamadas == ["adelantados", "sync", "auditar", "alerta"]


CAPTURA_AZTECA = (
    "Enviaste a una cuenta $140.00 02/Oct/2026 13:37:33 (CST) Cuenta origen Guardadito ***6745 "
    "Cuenta destino ARISEL F******** Banco Azteca ***265 Folio: MX100000001"
)


def test_la_captura_de_la_app_azteca_trae_folio_y_hora():
    datos = OCRService.extraer_datos(CAPTURA_AZTECA)
    assert datos["folio"] == "MX100000001"
    assert datos["monto"] == 140.0
    assert datos["fecha_pago"] == datetime(2026, 10, 2, 13, 37, 33)


def test_azteca_a_azteca_no_se_audita_con_el_correo(monkeypatch):
    # Transferencia interna: el banco no manda correo y no debe alertar "sin depósito".
    revision = _revision(folio_detectado="MX100000001", huella_captura=None)
    resultado, revision, *_ = _aplicar(monkeypatch, revision=revision)
    assert resultado["aplicado"] is True
    assert revision.auditoria_banco == "interna_azteca"


def test_un_spei_de_otro_banco_se_sigue_auditando(monkeypatch):
    revision = _revision(folio_detectado="12345P05202610020000000001", huella_captura=None)
    _, revision, *_ = _aplicar(monkeypatch, revision=revision)
    assert revision.auditoria_banco == "pendiente"
