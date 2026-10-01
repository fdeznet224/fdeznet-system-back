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
CONFIG = SimpleNamespace(validar_pagos_con="captura", ventana_dias=3, cuentas_destino_permitidas="6342,5265")
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
        transaccion_correo_id=None,
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
    def __init__(self, revision, clave_usada=False):
        self.revision = revision
        self.clave_usada = clave_usada
        self.agregados = []

    async def scalar(self, _consulta):
        return 99 if self.clave_usada else None

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


def _aplicar(monkeypatch, revision=None, factura=None, clave_usada=False, idempotente=False):
    cobros, alertas = [], []

    class _Billing:
        def __init__(self, _db):
            pass

        async def registrar_pago_completo(self, **kwargs):
            cobros.append(kwargs)
            return {"pago_id": 700, "idempotente": idempotente}

    monkeypatch.setattr(comprobante_mod, "BillingService", _Billing)
    revision = revision or _revision()
    db = _DB(revision, clave_usada)

    async def avisar(numero, texto):
        alertas.append(texto)

    servicio = ComprobanteService(db, correo=SimpleNamespace(), avisar_admin=avisar)

    async def config_alertas(*_a):
        return SimpleNamespace(telefonos_alerta="9611111111")

    monkeypatch.setattr(db, "get", lambda modelo, llave: (
        config_alertas() if modelo.__name__ == "ConfiguracionSistema" else asyncio.sleep(0, revision)
    ))
    resultado = asyncio.run(servicio.aplicar_por_captura(revision, CLIENTE, factura or _factura(), CONFIG))
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
