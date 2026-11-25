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
    # Sin preguntarle el número no se manda nada.
    assert "444 123 4567" in asyncio.run(contexto.usar("cobrar_por_codi", {}))["error"]
    assert enviados == []
    contexto.conversacion = "EMPRESA: ¿Te lo mando al 444 123 4567?\nCLIENTE: sí"
    assert asyncio.run(contexto.usar("cobrar_por_codi", {}))["enviado"] is True
    assert enviados == [(7, "4441234567", "whatsapp")]


# ------------------------------------------------------- celular del banco
def test_celular_para_codi_prefiere_el_que_ya_funciono():
    cliente = SimpleNamespace(telefono_codi="9611234567", telefono="9610000000")
    assert codi.celular_para_codi(cliente, "5219613699652@c.us") == "9611234567"
    cliente.telefono_codi = None
    assert codi.celular_para_codi(cliente, "5219613699652@c.us") == "9613699652"
    assert codi.celular_para_codi(cliente, "123456789012345@lid") == "9610000000"
    assert codi.celular_legible("9613699652") == "961 369 9652"


def test_el_pago_guarda_el_celular_del_banco(monkeypatch):
    _config(monkeypatch)
    _pagos(monkeypatch)
    cliente = SimpleNamespace(id=7, nombre="Juana Pérez", cedula="329B", telefono_codi=None)
    cobro = CobroCodiModel(id=41, cliente_id=7, tipo="celular", telefono="9611234567", monto=Decimal("350"),
                           estado="enviado", pey_transaction_id="01TX")
    db = _DB(cobro=cobro, cliente=cliente)
    cuerpo = _aviso()
    assert asyncio.run(codi.procesar_webhook(db, _firmar(cuerpo), cuerpo))["aplicado"] is True
    assert cliente.telefono_codi == "9611234567"


def test_el_agente_usa_el_celular_que_ya_funciono(monkeypatch):
    enviados = []

    async def cobrar(_db, cliente, numero, origen="whatsapp"):
        enviados.append(numero)
        return {"enviado": True}

    import src.application.services.agente_ia_service as agente_mod

    monkeypatch.setattr(agente_mod, "cobrar_al_celular", cobrar)
    contexto = _Contexto(AgenteIAService(_DB()), SimpleNamespace(mensaje_chat_id=1), "automatico",
                         "5214441234567@c.us", None, None)
    contexto.cliente = SimpleNamespace(**vars(CLIENTE), telefono_codi="9611234567")
    contexto.conversacion = "EMPRESA: ¿Te lo mando al 961-123-4567?\nCLIENTE: no, al 9619998877"
    asyncio.run(contexto.usar("cobrar_por_codi", {}))
    asyncio.run(contexto.usar("cobrar_por_codi", {"telefono": "9619998877"}))
    assert enviados == ["9611234567", "9619998877"]


def test_el_agente_confirma_el_celular_antes_de_cobrar():
    from src.application.services.agente_ia_service import HERRAMIENTA_CODI, REGLA_CODI

    assert HERRAMIENTA_CODI["function"]["parameters"]["required"] == ["telefono"]
    assert "SOLO después de que confirme" in REGLA_CODI
    assert "datos_de_pago" in REGLA_CODI  # si pide la cuenta, también se la da


# ------------------------------------------------------- responde PAGAR
def test_responde_pagar_solo_con_codi_y_agente(monkeypatch):
    import src.application.services.bot_flow_service as bot
    import src.application.services.notification_service as avisos

    estado = {"codi": True, "modo": "automatico"}

    async def disponible(_db, _cliente=None):
        return estado["codi"]

    async def config_bot(_db):
        return SimpleNamespace(agente_modo=estado["modo"])

    monkeypatch.setattr(codi, "codi_disponible", disponible)
    monkeypatch.setattr(bot, "get_or_create_bot_config", config_bot)
    assert asyncio.run(avisos.invitacion_codi(None)) == avisos.TEXTO_PAGAR_CODI
    estado["modo"] = "apagado"
    assert asyncio.run(avisos.invitacion_codi(None)) == ""
    estado.update(codi=False, modo="automatico")
    assert asyncio.run(avisos.invitacion_codi(None)) == ""


def test_la_notificacion_del_portal_no_dice_responde_pagar():
    from src.application.services.notification_service import TEXTO_PAGAR_CODI
    from src.application.services.push_service import texto_de_whatsapp

    texto = texto_de_whatsapp(f"Tu pago vence el 5.\n\n{TEXTO_PAGAR_CODI}")
    assert "PAGAR" not in texto and texto.startswith("Tu pago vence el 5.")


# ------------------------------------------------------- modo prueba
def test_en_prueba_solo_los_contratos_de_la_lista():
    config = SimpleNamespace(codi_activo=True, pey_api_key="sk", codi_solo_contratos="187B, 329b")
    assert codi.codi_para(config, SimpleNamespace(cedula="187b")) is True
    assert codi.codi_para(config, SimpleNamespace(cedula="329B")) is True
    assert codi.codi_para(config, SimpleNamespace(cedula="F1C8")) is False
    assert codi.codi_para(config, None) is False
    config.codi_solo_contratos = None
    assert codi.codi_para(config, SimpleNamespace(cedula="F1C8")) is True
    config.codi_activo = False
    assert codi.codi_para(config, SimpleNamespace(cedula="187B")) is False


def test_en_prueba_no_le_cobra_a_otro_contrato(monkeypatch):
    config = _config(monkeypatch)
    config.codi_solo_contratos = "187B"
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _p: pytest.fail("no debe llamar a pey")))
    resultado = asyncio.run(codi.cobrar_al_celular(_DB(), CLIENTE, "4441234567", http=http))
    assert "no está activo" in resultado["error"]


# ------------------------------------------------------- QR y respaldo
class _Lista:
    def __init__(self, valores):
        self.valores = valores

    def scalars(self):
        return self

    def all(self):
        return self.valores

    def first(self):
        return self.valores[0] if self.valores else None


class _DBRevision(_DB):
    """Devuelve los cobros pendientes a la revisión y guarda los cambios de estado."""

    def __init__(self, cobros):
        super().__init__()
        self.cobros = {c.id: c for c in cobros}

    async def execute(self, sentencia):
        return _Lista(list(self.cobros.values()))

    async def get(self, modelo, llave):
        return self.cobros.get(llave) if modelo is CobroCodiModel else None


def test_la_revision_aplica_lo_pagado_y_vence_lo_viejo(monkeypatch):
    from datetime import datetime, timedelta

    _config(monkeypatch)
    ahora = datetime.now()
    pagado = CobroCodiModel(id=1, cliente_id=7, tipo="celular", estado="enviado", pey_transaction_id="TX1",
                            monto=Decimal("350"), creado_en=ahora - timedelta(minutes=5), vence_en=ahora + timedelta(minutes=10))
    viejo = CobroCodiModel(id=2, cliente_id=7, tipo="celular", estado="enviado", pey_transaction_id="TX2",
                           monto=Decimal("350"), creado_en=ahora - timedelta(hours=1), vence_en=ahora - timedelta(minutes=40))
    qr = CobroCodiModel(id=3, cliente_id=7, tipo="qr", estado="enviado", codi_id="IDC9",
                        monto=Decimal("350"), creado_en=ahora - timedelta(minutes=5), vence_en=ahora + timedelta(hours=20))
    aplicados = []

    async def aplicar(_db, **datos):
        aplicados.append((datos["transaccion"], datos["es_qr"], datos["monto"]))
        return {"aplicado": True}

    monkeypatch.setattr(codi, "aplicar_pago_codi", aplicar)

    def pey(peticion):
        ruta = peticion.url.path
        if ruta == "/transaction/TX1":
            return httpx.Response(200, json={"id": "TX1", "status": "PAID", "amount": 350, "datePaid": 1790724818})
        if ruta == "/transaction/TX2":
            return httpx.Response(200, json={"id": "TX2", "status": "PENDING", "amount": 350})
        if ruta == "/transaction/codi/qr/IDC9":
            return httpx.Response(200, json={"items": [{"id": "TXQ", "status": "PAID", "amount": 350}], "nextToken": None})
        return httpx.Response(404)

    db = _DBRevision([pagado, viejo, qr])
    http = httpx.AsyncClient(transport=httpx.MockTransport(pey))
    assert asyncio.run(codi.revisar_cobros_pendientes(db, http=http)) == 2
    assert aplicados == [("TX1", False, Decimal("350.00")), ("TXQ", True, Decimal("350.00"))]
    assert viejo.estado == "vencido" and qr.estado == "enviado"


def test_un_qr_pagado_dos_veces_se_registra_dos_veces(monkeypatch):
    _config(monkeypatch)
    llamadas, _ = _pagos(monkeypatch)
    qr = CobroCodiModel(id=41, cliente_id=7, tipo="qr", codi_id="IDC9", monto=Decimal("350"), estado="aplicado",
                        pey_transaction_id="TXA", origen="portal")
    db = _DB(cobro=None, cliente=CLIENTE)

    async def execute(sentencia):
        texto = str(sentencia)
        if "codi_id" in texto.split("WHERE")[-1]:
            return _Resultado(qr)
        return _Resultado(None)

    db.execute = execute
    resultado = asyncio.run(codi.aplicar_pago_codi(db, es_qr=True, transaccion="TXB", monto=Decimal("350"),
                                                   codi_qr_id="IDC9"))
    assert resultado["aplicado"] is True
    nuevo = [o for o in db.agregados if isinstance(o, CobroCodiModel)][0]
    assert nuevo.pey_transaction_id == "TXB" and nuevo.codi_id == "IDC9" and nuevo.cliente_id == 7
    assert llamadas[0][4] == "codi:TXB"


def test_crea_el_qr_por_lo_que_debe(monkeypatch):
    _config(monkeypatch)
    _deuda(monkeypatch, 350)
    enviados = []

    def pey(peticion):
        enviados.append(json.loads(peticion.content))
        assert peticion.url.path == "/codi/qr"
        return httpx.Response(201, json={"id": "IDC1", "qrString": '{"TYP":19,"v":{"DEV":"x"}}'})

    db = _DB()
    http = httpx.AsyncClient(transport=httpx.MockTransport(pey))
    datos = asyncio.run(codi.crear_qr(db, CLIENTE, http=http))
    assert datos["monto"] == "350.00" and base64.b64decode(datos["qr_png"]).startswith(b"\x89PNG")
    assert enviados[0]["amount"] == 350 and enviados[0]["expiration"] == 86400
    cobro = [o for o in db.agregados if isinstance(o, CobroCodiModel)][0]
    assert cobro.tipo == "qr" and cobro.codi_id == "IDC1" and cobro.vence_en is not None


def test_el_agente_manda_el_qr_como_imagen(monkeypatch, tmp_path):
    import src.application.services.agente_ia_service as agente_mod

    async def qr(_db, cliente, origen="whatsapp"):
        return {"cobro_id": 5, "monto": "350.00", "vence_en": "2026-11-26T10:00",
                "qr_png": base64.b64encode(b"\x89PNGfalso").decode()}

    monkeypatch.setattr(agente_mod, "crear_qr", qr)
    monkeypatch.chdir(tmp_path)
    mandados = []

    async def enviar(telefono, texto, ruta=None):
        mandados.append((telefono, texto, open(ruta, "rb").read()))
        return True

    contexto = _Contexto(AgenteIAService(_DB(), enviar=enviar), SimpleNamespace(mensaje_chat_id=1), "automatico",
                         "5214441234567@c.us", None, None)
    contexto.cliente = CLIENTE
    resultado = asyncio.run(contexto.usar("mandar_qr_codi", {}))
    assert resultado["enviado"] is True
    assert mandados[0][0] == "5214441234567@c.us" and "329B" in mandados[0][1] and mandados[0][2] == b"\x89PNGfalso"
