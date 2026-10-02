"""Aprobar un comprobante cuando el correo del banco no llega."""

import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import src.jobs as jobs
from src.interfaces.api import whatsapp

ADMIN = SimpleNamespace(id=1, rol="admin", usuario="arisel")
CAJERO = SimpleNamespace(id=5, rol="cajero", usuario="caja")
FACTURA = SimpleNamespace(id=1134, cliente_id=248)
CLIENTE = SimpleNamespace(id=248, nombre="Margarita Moreno Lopez")


class _DB:
    def __init__(self, comprobante, folio_usado=False):
        self.comprobante = comprobante
        self.folio_usado = folio_usado
        self.agregados = []

    async def get(self, modelo, _llave):
        return {
            "ComprobantePagoRevisionModel": self.comprobante,
            "FacturaModel": FACTURA,
            "ClienteModel": CLIENTE,
            "ConfiguracionSistema": SimpleNamespace(telefonos_alerta="9611111111"),
        }.get(modelo.__name__)

    async def scalar(self, _consulta):
        return 7 if self.folio_usado else None

    def add(self, objeto):
        self.agregados.append(objeto)

    async def commit(self):
        return None

    async def rollback(self):
        return None


def _comprobante(**cambios):
    datos = dict(id=13, estado="pendiente", folio_detectado=None, telefono="95133743751296@lid",
                 pago_id=None, motivo_revision="sin_referencia", notas_revision=None)
    datos.update(cambios)
    return SimpleNamespace(**datos)


def _aprobar(monkeypatch, usuario=ADMIN, notas="App Azteca 30/09 08:58 $300", folio_usado=False, **datos):
    avisos, cobros = [], []

    class _Billing:
        def __init__(self, _db):
            pass

        async def registrar_pago_completo(self, **kwargs):
            cobros.append(kwargs)
            return {"pago_id": 900}

    class _WA:
        async def enviar_mensaje(self, **kwargs):
            avisos.append(kwargs)

    monkeypatch.setattr(whatsapp, "BillingService", _Billing)
    monkeypatch.setattr(whatsapp, "WhatsAppService", _WA)
    comprobante = _comprobante(**datos.pop("comprobante", {}))
    db = _DB(comprobante, folio_usado)
    solicitud = whatsapp.AprobarComprobanteRequest(
        cliente_id=248, factura_id=1134, monto=Decimal("300.00"), notas=notas,
        verificado_en_banco=True, **datos,
    )
    resultado = asyncio.run(whatsapp._aprobar_verificado_en_banco(db, comprobante, solicitud, usuario))
    return resultado, comprobante, db, cobros, avisos


def test_un_administrador_aprueba_lo_que_verifico_en_el_banco(monkeypatch):
    resultado, comprobante, _, cobros, avisos = _aprobar(monkeypatch, referencia="M01576668")
    assert resultado["verificado_en_banco"] is True
    assert cobros[0]["monto"] == Decimal("300.00") and cobros[0]["referencia"] == "M01576668"
    assert comprobante.estado == "aprobado" and comprobante.pago_id == 900
    assert comprobante.motivo_revision == "verificado_en_banco_sin_correo"
    assert comprobante.notas_revision == "Verificado en el banco por arisel: App Azteca 30/09 08:58 $300"
    assert "sin correo del banco" in avisos[0]["mensaje"] and "arisel" in avisos[0]["mensaje"]


def test_el_folio_queda_bloqueado_para_otro_pago(monkeypatch):
    _, _, db, _, _ = _aprobar(monkeypatch, referencia="M01576668")
    assert db.agregados[0].folio_banco == "M01576668"


def test_solo_un_administrador_puede_aprobar_sin_correo(monkeypatch):
    with pytest.raises(HTTPException) as error:
        _aprobar(monkeypatch, usuario=CAJERO)
    assert error.value.status_code == 403


def test_hay_que_escribir_donde_se_verifico(monkeypatch):
    with pytest.raises(HTTPException) as error:
        _aprobar(monkeypatch, notas="ok")
    assert error.value.status_code == 400


def test_no_se_aprueba_un_folio_ya_usado(monkeypatch):
    with pytest.raises(HTTPException) as error:
        _aprobar(monkeypatch, referencia="M01576668", folio_usado=True)
    assert error.value.status_code == 409


# ------------------------------------------------ aviso a las 2 horas
def test_se_avisa_una_sola_vez_por_comprobante(monkeypatch):
    enviados = []

    async def pendientes(_db, ya_avisados):
        return [p for p in [{"id": 17, "cliente": "Agustín (EC15)", "monto": Decimal("300"), "folio": None}]
                if p["id"] not in ya_avisados]

    async def enviar(mensaje, _db, tipo_evento="alerta_router"):
        enviados.append((mensaje, tipo_evento))

    monkeypatch.setattr(jobs.BankEmailService, "comprobantes_sin_correo", staticmethod(pendientes))
    monkeypatch.setattr(jobs, "enviar_alertas_whatsapp", enviar)
    monkeypatch.setattr(jobs, "_avisados_sin_correo", set())
    assert asyncio.run(jobs.avisar_comprobantes_sin_correo(None)) == 1
    assert asyncio.run(jobs.avisar_comprobantes_sin_correo(None)) == 0
    assert "Comprobante #17" in enviados[0][0] and enviados[0][1] == "alerta_comprobante"


# ------------------------------------------- modo captura: aprobar en el panel
def _aprobar_captura(monkeypatch, usuario=CAJERO, folio_usado=False, idempotente=False, comprobante=None, **datos):
    cobros = []

    class _Billing:
        def __init__(self, _db):
            pass

        async def registrar_pago_completo(self, **kwargs):
            cobros.append(kwargs)
            return {"pago_id": 901, "idempotente": idempotente}

    monkeypatch.setattr(whatsapp, "BillingService", _Billing)
    comprobante = _comprobante(**{"folio_detectado": "000000254", "monto_detectado": Decimal("420.00"),
                                  "huella_captura": None, "auditoria_banco": None, **(comprobante or {})})
    db = _DB(comprobante, folio_usado)
    solicitud = whatsapp.AprobarComprobanteRequest(cliente_id=248, factura_id=1134, **datos)
    resultado = asyncio.run(whatsapp._aprobar_por_captura_en_panel(db, comprobante, solicitud, usuario))
    return resultado, comprobante, db, cobros


def test_en_modo_captura_se_aprueba_sin_deposito_del_banco(monkeypatch):
    # Pago fuera de horario: el banco no mandó correo.
    resultado, comprobante, db, cobros = _aprobar_captura(monkeypatch)
    assert resultado["por_captura"] is True
    assert cobros[0]["monto"] == Decimal("420.00")
    assert cobros[0]["clave_idempotencia"] == "captura:000000254"
    assert comprobante.estado == "aprobado" and comprobante.pago_id == 901
    assert comprobante.motivo_revision == "aprobado_en_panel_por_captura"
    assert comprobante.notas_revision == "Aprobado con la captura por caja"
    assert comprobante.auditoria_banco == "pendiente"
    assert db.agregados[0].folio_banco == "000000254"


def test_en_modo_captura_azteca_a_azteca_no_se_audita(monkeypatch):
    _, comprobante, _, _ = _aprobar_captura(monkeypatch, comprobante={"folio_detectado": "MX100000001"})
    assert comprobante.auditoria_banco == "interna_azteca"


def test_en_modo_captura_no_se_aprueba_una_captura_ya_usada(monkeypatch):
    with pytest.raises(HTTPException) as error:
        _aprobar_captura(monkeypatch, folio_usado=True)
    assert error.value.status_code == 409


def test_en_modo_captura_sin_folio_ni_huella_pide_la_referencia(monkeypatch):
    with pytest.raises(HTTPException) as error:
        _aprobar_captura(monkeypatch, comprobante={"folio_detectado": None})
    assert error.value.status_code == 400
    resultado, _, _, cobros = _aprobar_captura(monkeypatch, comprobante={"folio_detectado": None}, referencia="ABC123456")
    assert cobros[0]["referencia"] == "ABC123456"


def test_en_modo_captura_si_el_cobro_ya_existia_no_se_aprueba_dos_veces(monkeypatch):
    with pytest.raises(HTTPException) as error:
        _aprobar_captura(monkeypatch, idempotente=True)
    assert error.value.status_code == 409
