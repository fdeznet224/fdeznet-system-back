"""Cobro CoDi con pey.mx: envío del cobro, firma del webhook y pago aplicado."""

import asyncio
import base64
import hashlib
import hmac
import json
import time
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

import src.application.services.codi_service as codi
from src.application.services.agente_ia_service import HERRAMIENTAS, AgenteIAService, _Contexto
from src.application.services.billing_service import BillingService
from src.infrastructure.models import ClienteModel, CobroCodiModel

SECRETO = "whsec_" + base64.b64encode(b"secreto-de-prueba-1234").decode()


def _firmar(cuerpo: bytes, secreto=SECRETO, marca=None, mensaje_id="msg_1"):
    marca = str(int(marca or time.time()))
    llave = base64.b64decode(secreto[len("whsec_"):])
    firma = base64.b64encode(hmac.new(llave, f"{mensaje_id}.{marca}.".encode() + cuerpo, hashlib.sha256).digest()).decode()
    return {"webhook-id": mensaje_id, "webhook-timestamp": marca, "webhook-signature": f"v1,{firma}"}


class _Resultado:
    def __init__(self, valor):
        self.valor = valor

    def scalars(self):
        return self

    def first(self):
        return self.valor


class _DB:
    def __init__(self, cobro=None, cliente=None, reciente=None):
        self.cobro = cobro
        self.cliente = cliente
        self.reciente = reciente
        self.agregados = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, sentencia):
        texto = str(sentencia)
        if "pey_transaction_id" in texto.split("WHERE")[-1]:
            return _Resultado(self.cobro)
        return _Resultado(self.reciente)

    async def get(self, modelo, llave):
        if modelo is CobroCodiModel:
            return self.cobro if self.cobro and self.cobro.id == llave else None
        if modelo is ClienteModel:
            return self.cliente if self.cliente and self.cliente.id == llave else None
        return None

    def add(self, objeto):
        self.agregados.append(objeto)

    async def flush(self):
        for objeto in self.agregados:
            if isinstance(objeto, CobroCodiModel) and objeto.id is None:
                objeto.id = 41

    async def refresh(self, _objeto):
        return None

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def _config(monkeypatch, activo=True, api_key="sk_live", secreto=SECRETO):
    config = SimpleNamespace(codi_activo=activo, pey_api_key=api_key, pey_webhook_secreto=secreto)

    async def obtener(_db):
        return config

    monkeypatch.setattr(codi, "config_pagos", obtener)
    return config


CLIENTE = SimpleNamespace(id=7, nombre="Juana Pérez", cedula="329B", estado="suspendido")


# ------------------------------------------------------------------ firma
def test_firma_valida_y_rechazos():
    cuerpo = b'{"type":"codi.pn.payment_received"}'
    codi.verificar_firma(SECRETO, _firmar(cuerpo), cuerpo)
    with pytest.raises(codi.FirmaInvalida):
        codi.verificar_firma(SECRETO, _firmar(cuerpo), cuerpo + b" ")
    with pytest.raises(codi.FirmaInvalida):
        codi.verificar_firma(SECRETO, _firmar(cuerpo, marca=time.time() - 3600), cuerpo)
    with pytest.raises(codi.FirmaInvalida):
        codi.verificar_firma("", _firmar(cuerpo), cuerpo)
    otro = "whsec_" + base64.b64encode(b"otro").decode()
    with pytest.raises(codi.FirmaInvalida):
        codi.verificar_firma(otro, _firmar(cuerpo), cuerpo)


# ------------------------------------------------------------------ envío
def _deuda(monkeypatch, total):
    async def estado(self, _cliente_id):
        return {"total": total, "facturas": []}

    monkeypatch.setattr(BillingService, "estado_cuenta_cliente", estado)


def test_manda_el_cobro_por_lo_que_debe(monkeypatch):
    _config(monkeypatch)
    _deuda(monkeypatch, Decimal("350.00"))
    enviados = []

    def responder(peticion):
        enviados.append((str(peticion.url), peticion.headers["Authorization"], json.loads(peticion.content)))
        return httpx.Response(200, json={"transactionId": "01TX", "codiId": "12345678901234567890"})

    db = _DB()
    http = httpx.AsyncClient(transport=httpx.MockTransport(responder))
    resultado = asyncio.run(codi.cobrar_al_celular(db, CLIENTE, "+52 1 444 123 4567", http=http))

    assert resultado["enviado"] is True and resultado["monto"] == "350.00"
    url, autorizacion, cuerpo = enviados[0]
    assert url.endswith("/codi/pn") and autorizacion == "Bearer sk_live"
    assert cuerpo["phoneNumber"] == "4441234567" and cuerpo["amount"] == 350.0
    assert cuerpo["numRef"] == "41" and cuerpo["expiration"] >= 360
    cobro = db.agregados[0]
    assert cobro.pey_transaction_id == "01TX" and cobro.estado == "enviado"


def test_sin_codi_activo_o_sin_deuda_no_manda_nada(monkeypatch):
    _config(monkeypatch, activo=False)
    assert "error" in asyncio.run(codi.cobrar_al_celular(_DB(), CLIENTE, "4441234567"))
    _config(monkeypatch)
    _deuda(monkeypatch, Decimal("0"))
    assert "saldo" in asyncio.run(codi.cobrar_al_celular(_DB(), CLIENTE, "4441234567"))["error"]


def test_si_pey_rechaza_queda_el_error(monkeypatch):
    _config(monkeypatch)
    _deuda(monkeypatch, Decimal("300"))
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _p: httpx.Response(400, json={"message": "bad phone"})))
    db = _DB()
    resultado = asyncio.run(codi.cobrar_al_celular(db, CLIENTE, "4441234567", http=http))
    assert "error" in resultado
    assert db.agregados[0].estado == "error" and "400" in db.agregados[0].detalle


def test_no_repite_el_cobro_si_se_pide_enseguida(monkeypatch):
    _config(monkeypatch)
    _deuda(monkeypatch, Decimal("300"))
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _p: pytest.fail("no debe llamar a pey")))
    db = _DB(reciente=SimpleNamespace(id=3))
    resultado = asyncio.run(codi.cobrar_al_celular(db, CLIENTE, "4441234567", http=http))
    assert resultado["ya_enviado_hace_un_momento"] is True


# ------------------------------------------------------------------ webhook
def _aviso(**cambios):
    datos = {
        "amount": 350, "apiKeyId": "01K", "concept": "Internet contrato 329B", "datePaid": 1790724818,
        "metadata": {"trackingKey": "0192191042", "externalMetadata": {"cobro_id": 41}},
        "numRef": "41", "transactionId": "01TX", "type": "codi.pn.payment_received",
    }
    datos.update(cambios)
    return json.dumps(datos).encode()


def _pagos(monkeypatch, error=None):
    llamadas = []

    async def registrar(self, cliente_id, usuario, metodo, monto, referencia=None, clave_idempotencia=None):
        llamadas.append((cliente_id, metodo, monto, referencia, clave_idempotencia))
        if error:
            raise ValueError(error)
        return {"pago_ids": [900, 901]}

    async def usuario(_db, nombre):
        return SimpleNamespace(id=99, usuario=nombre)

    avisos = []

    async def avisar(_db, texto):
        avisos.append(texto)

    monkeypatch.setattr(BillingService, "registrar_pago_cliente", registrar)
    monkeypatch.setattr(codi, "usuario_de_sistema", usuario)
    monkeypatch.setattr(codi, "_avisar_personal", avisar)
    return llamadas, avisos


def test_el_aviso_de_pey_aplica_el_pago(monkeypatch):
    _config(monkeypatch)
    llamadas, avisos = _pagos(monkeypatch)
    cobro = CobroCodiModel(id=41, cliente_id=7, monto=Decimal("350"), estado="enviado", pey_transaction_id="01TX")
    db = _DB(cobro=cobro, cliente=CLIENTE)
    cuerpo = _aviso()

    resultado = asyncio.run(codi.procesar_webhook(db, _firmar(cuerpo), cuerpo))

    assert resultado["aplicado"] is True
    assert llamadas == [(7, "codi", Decimal("350.00"), "CoDi 0192191042", "codi:01TX")]
    assert cobro.estado == "aplicado" and cobro.pago_ids == "900,901"
    assert avisos == []

    # pey reintenta el mismo aviso: no se cobra otra vez.
    assert asyncio.run(codi.procesar_webhook(db, _firmar(cuerpo), cuerpo))["repetido"] is True
    assert len(llamadas) == 1


def test_aviso_sin_firma_no_aplica_nada(monkeypatch):
    _config(monkeypatch)
    llamadas, _ = _pagos(monkeypatch)
    cuerpo = _aviso()
    with pytest.raises(codi.FirmaInvalida):
        asyncio.run(codi.procesar_webhook(_DB(), {"webhook-id": "x"}, cuerpo))
    assert llamadas == []


def test_pago_que_no_se_puede_aplicar_avisa_al_personal(monkeypatch):
    _config(monkeypatch)
    llamadas, avisos = _pagos(monkeypatch, error="El cliente no tiene saldo pendiente")
    cobro = CobroCodiModel(id=41, cliente_id=7, monto=Decimal("350"), estado="enviado", pey_transaction_id="01TX")
    db = _DB(cobro=cobro, cliente=CLIENTE)
    cuerpo = _aviso()

    resultado = asyncio.run(codi.procesar_webhook(db, _firmar(cuerpo), cuerpo))

    assert resultado["aplicado"] is False and cobro.estado == "sin_aplicar"
    assert "Juana Pérez" in avisos[0] and "$350.00" in avisos[0]


def test_pago_de_un_cobro_desconocido_avisa_al_personal(monkeypatch):
    _config(monkeypatch)
    llamadas, avisos = _pagos(monkeypatch)
    cuerpo = _aviso(transactionId="01OTRO", metadata={})
    resultado = asyncio.run(codi.procesar_webhook(_DB(), _firmar(cuerpo), cuerpo))
    assert resultado == {"aplicado": False, "motivo": "sin_cliente"}
    assert llamadas == [] and "sin aplicar" in avisos[0]


# ------------------------------------------------------------------ agente
def test_el_agente_solo_ofrece_codi_si_esta_activo():
    assert "cobrar_por_codi" not in {h["function"]["name"] for h in HERRAMIENTAS}


def test_el_agente_pide_el_numero_si_el_chat_es_lid():
    contexto = _Contexto(AgenteIAService(_DB()), SimpleNamespace(mensaje_chat_id=1), "automatico",
                         "123456789012345@lid", None, None)
    contexto.cliente = CLIENTE
    resultado = asyncio.run(contexto.usar("cobrar_por_codi", {}))
    assert "celular" in resultado["error"]


def test_el_agente_manda_el_cobro_al_numero_del_chat(monkeypatch):
    enviados = []

    async def cobrar(_db, cliente, numero, origen="whatsapp"):
        enviados.append((cliente.id, numero, origen))
        return {"enviado": True}

    import src.application.services.agente_ia_service as agente_mod

    monkeypatch.setattr(agente_mod, "cobrar_al_celular", cobrar)
    contexto = _Contexto(AgenteIAService(_DB()), SimpleNamespace(mensaje_chat_id=1), "automatico",
                         "5214441234567@c.us", None, None)
    contexto.cliente = CLIENTE
    assert asyncio.run(contexto.usar("cobrar_por_codi", {}))["enviado"] is True
    assert enviados == [(7, "4441234567", "whatsapp")]
