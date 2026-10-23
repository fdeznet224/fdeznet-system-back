"""Portal del cliente: calendario de pago, cambio de teléfono con código y notificaciones push."""

import asyncio
import base64
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import http_ece
import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from py_vapid import Vapid02

from src.application.services import portal_cliente_service as portal
from src.application.services import push_service as push
from src.application.services.portal_cliente_service import PortalClienteService, cuenta_regresiva, telefono_mexico
from src.infrastructure import auth
from src.infrastructure.models import ClienteModel, PortalAccesoModel


def factura(**datos):
    base = {"es_promesa_activa": False, "fecha_promesa_pago": None, "fecha_vencimiento": None, "fecha_limite_corte": None}
    return SimpleNamespace(**{**base, **datos})


def test_cuenta_regresiva_de_pago():
    hoy = date(2026, 10, 23)
    assert cuenta_regresiva(None, hoy, date(2026, 11, 1)) == {"tipo": "al_corriente", "fecha": "2026-11-01", "dias": 9}
    por_vencer = cuenta_regresiva(factura(fecha_vencimiento=date(2026, 10, 26), fecha_limite_corte=date(2026, 10, 29)), hoy, None)
    assert (por_vencer["tipo"], por_vencer["dias"], por_vencer["corte"]) == ("por_vencer", 3, "2026-10-29")
    # Ya venció: lo que cuenta es cuántos días faltan para la suspensión.
    vencida = cuenta_regresiva(factura(fecha_vencimiento=date(2026, 10, 20), fecha_limite_corte=date(2026, 10, 26)), hoy, None)
    assert (vencida["tipo"], vencida["dias"]) == ("por_suspender", 3)
    assert cuenta_regresiva(factura(fecha_vencimiento=date(2026, 10, 1), fecha_limite_corte=date(2026, 10, 5)), hoy, None)["tipo"] == "vencido"
    promesa = cuenta_regresiva(factura(es_promesa_activa=True, fecha_promesa_pago=date(2026, 10, 30)), hoy, None)
    assert (promesa["tipo"], promesa["dias"]) == ("promesa", 7)


def test_telefono_a_10_digitos():
    assert telefono_mexico("+52 1 993 123 4567") == "9931234567"
    assert telefono_mexico("52 993-123-4567") == "9931234567"
    assert telefono_mexico("(993) 123 4567") == "9931234567"
    with pytest.raises(ValueError):
        telefono_mexico("12345")


class BaseFalsa:
    def __init__(self, cliente, acceso):
        self.cliente, self.acceso, self.agregados = cliente, acceso, []

    async def get(self, modelo, _llave):
        return self.cliente if modelo is ClienteModel else self.acceso

    def add(self, objeto):
        self.agregados.append(objeto)

    async def commit(self):
        pass

    async def refresh(self, objeto):
        objeto.id = 99


@pytest.fixture
def con_codigo(monkeypatch):
    monkeypatch.setattr(auth, "get_password_hash", lambda clave: f"hash:{clave}")
    monkeypatch.setattr(auth, "verify_password", lambda clave, guardado: guardado == f"hash:{clave}")
    monkeypatch.setattr(portal.secrets, "randbelow", lambda _n: 4321)
    tareas = []

    async def agregar(tarea):
        tareas.append(tarea)

    from src.infrastructure import whatsapp_client
    monkeypatch.setattr(whatsapp_client.whatsapp_queue, "agregar_tarea", agregar)
    cliente = SimpleNamespace(id=327, telefono="9930000000")
    acceso = PortalAccesoModel(cliente_id=327, password_hash="x", version=1, codigo_intentos=0)
    return PortalClienteService(BaseFalsa(cliente, acceso)), cliente, acceso, tareas


def test_el_telefono_cambia_solo_con_el_codigo_que_llega_al_numero_nuevo(con_codigo):
    servicio, cliente, acceso, tareas = con_codigo
    asyncio.run(servicio.pedir_codigo_telefono(cliente, "52 993 111 2222"))
    mensaje = servicio.db.agregados[0]
    assert mensaje.telefono == "9931112222" and "004321" in mensaje.mensaje
    # Sin cliente_id: si el número nuevo no tiene WhatsApp no se manda a su chat de siempre.
    assert mensaje.cliente_id is None and tareas == [{"mensaje_chat_id": 99, "intervalo": 0, "tipo_evento": "codigo_portal"}]
    with pytest.raises(ValueError, match="no es correcto"):
        asyncio.run(servicio.confirmar_codigo_telefono(cliente, "000000"))
    assert cliente.telefono == "9930000000"
    assert asyncio.run(servicio.confirmar_codigo_telefono(cliente, "004321")) == {"telefono": "9931112222"}
    assert acceso.telefono_pendiente is None and acceso.codigo_hash is None


def test_el_codigo_vence_y_se_bloquea_por_intentos(con_codigo):
    servicio, cliente, acceso, _ = con_codigo
    with pytest.raises(ValueError, match="ya es tu número"):
        asyncio.run(servicio.pedir_codigo_telefono(cliente, "9930000000"))
    asyncio.run(servicio.pedir_codigo_telefono(cliente, "9931112222"))
    acceso.codigo_expira = datetime.now() - timedelta(minutes=1)
    with pytest.raises(ValueError, match="venció"):
        asyncio.run(servicio.confirmar_codigo_telefono(cliente, "004321"))
    asyncio.run(servicio.pedir_codigo_telefono(cliente, "9931112222"))
    acceso.codigo_intentos = portal.CODIGO_INTENTOS
    with pytest.raises(ValueError, match="venció"):
        asyncio.run(servicio.confirmar_codigo_telefono(cliente, "004321"))
    assert cliente.telefono == "9930000000"


def _b64(datos: bytes) -> str:
    return base64.urlsafe_b64encode(datos).rstrip(b"=").decode()


def celular():
    """Las llaves que genera el navegador al suscribirse."""
    privada = ec.generate_private_key(ec.SECP256R1())
    publica = privada.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    auth_secret = b"0123456789abcdef"
    return privada, _b64(publica), _b64(auth_secret), auth_secret


def test_la_notificacion_cifrada_la_descifra_el_celular():
    privada, p256dh, auth_b64, auth_secret = celular()
    cifrado = push.cifrar(b'{"titulo":"Pago recibido"}', p256dh, auth_b64)
    assert http_ece.decrypt(cifrado, private_key=privada, auth_secret=auth_secret, version="aes128gcm") == b'{"titulo":"Pago recibido"}'


def test_encabezado_vapid_para_el_servicio_del_navegador():
    llave = Vapid02()
    llave.generate_keys()
    privada = _b64(llave.private_key.private_numbers().private_value.to_bytes(32, "big"))
    encabezado = push.encabezado_vapid("https://fcm.googleapis.com/fcm/send/abc", {"privada": privada, "sujeto": "mailto:soporte@fdeznet.mx"})
    assert encabezado["Authorization"].startswith("vapid t=") and ",k=" in encabezado["Authorization"]


def test_texto_de_whatsapp_para_la_notificacion():
    assert push.texto_de_whatsapp("*Pago recibido*\n\nGracias por tu pago de _$350_") == "Pago recibido · Gracias por tu pago de $350"
    assert len(push.texto_de_whatsapp("x" * 500)) == push.LARGO_CUERPO


class Resultado:
    def __init__(self, filas):
        self.filas = filas

    def scalars(self):
        return self

    def all(self):
        return self.filas


class BasePush:
    def __init__(self, filas):
        self.filas, self.borradas = filas, []

    async def execute(self, _consulta):
        return Resultado(self.filas)

    async def delete(self, fila):
        self.borradas.append(fila)

    async def commit(self):
        pass


def test_enviar_borra_los_celulares_que_ya_no_existen(monkeypatch):
    monkeypatch.setenv("VAPID_SUBJECT", "mailto:soporte@fdeznet.mx")
    llave = Vapid02()
    llave.generate_keys()
    monkeypatch.setenv("VAPID_PRIVATE_KEY", _b64(llave.private_key.private_numbers().private_value.to_bytes(32, "big")))
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "publica")
    _, p256dh, auth_b64, _ = celular()
    vivo = SimpleNamespace(endpoint="https://push.example/vivo", p256dh=p256dh, auth=auth_b64, fallos=2, ultimo_envio=None)
    viejo = SimpleNamespace(endpoint="https://push.example/viejo", p256dh=p256dh, auth=auth_b64, fallos=0, ultimo_envio=None)
    recibidos = []

    def responder(request: httpx.Request) -> httpx.Response:
        recibidos.append(request)
        return httpx.Response(410 if request.url.path == "/viejo" else 201)

    async def probar():
        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            db = BasePush([vivo, viejo])
            enviadas = await push.PushService(db, http).enviar(327, "Pago recibido", "Gracias")
            return enviadas, db

    enviadas, db = asyncio.run(probar())
    assert enviadas == 1 and db.borradas == [viejo] and vivo.fallos == 0 and vivo.ultimo_envio
    assert recibidos[0].headers["Content-Encoding"] == "aes128gcm"


def test_solo_los_avisos_van_como_notificacion(monkeypatch):
    enviados = []

    class Falso:
        def __init__(self, _db):
            pass

        async def enviar(self, cliente_id, titulo, cuerpo):
            enviados.append((cliente_id, titulo, cuerpo))

    class Sesion:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_):
            return False

    monkeypatch.setattr(push, "PushService", Falso)
    from src.infrastructure import database
    monkeypatch.setattr(database, "SessionLocal", Sesion)
    asyncio.run(push.avisar_por_push(327, "bot_respuesta", "hola"))
    asyncio.run(push.avisar_por_push(None, "pago_recibido", "x"))
    asyncio.run(push.avisar_por_push(327, "pago_recibido", "*Pago recibido* de $350"))
    assert enviados == [(327, "Pago recibido", "Pago recibido de $350")]


class BaseConfig:
    def __init__(self):
        self.filas = []

    async def execute(self, _consulta):
        return Resultado(list(self.filas))

    def add(self, fila):
        self.filas.append(fila)

    async def commit(self):
        pass


def test_las_llaves_se_crean_una_vez_y_se_guardan(monkeypatch):
    for variable in ("VAPID_PRIVATE_KEY", "VAPID_PUBLIC_KEY", "VAPID_SUBJECT"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("PUBLIC_URL", "https://fdezpay.com/")
    db = BaseConfig()
    primera = asyncio.run(push.llaves(db))
    assert len(db.filas) == 2 and all(len(f.valor) <= 100 for f in db.filas)
    assert asyncio.run(push.llaves(db)) == primera and len(db.filas) == 2
    assert primera["sujeto"] == "https://fdezpay.com"
    # La privada firma y la pública es la que el navegador usa para suscribirse.
    firmado = push.encabezado_vapid("https://fcm.googleapis.com/fcm/send/x", primera)
    assert firmado["Authorization"].endswith(",k=" + primera["publica"])
    monkeypatch.setenv("PUBLIC_URL", "http://10.0.0.5")
    assert push.sujeto() == "mailto:soporte@localhost"


CONOCIMIENTO = """## Datos de pago
- Usa la herramienta `datos_de_pago`: no es un lugar.

## Pago en efectivo (preguntar primero su zona o colonia)
- Flores Magón: casa de Clari, barrio La Bomba.
- Vicente Guerrero: por la salida a Ponciano, frente a la purificadora.
- Paraíso y las demás colonias: en Villa de Guadalupe, tienda de doña Yoli.

## Cobros
- Reconexión: $30.
"""


def test_lugar_de_pago_en_efectivo_segun_su_zona():
    assert portal.pago_en_efectivo(CONOCIMIENTO, "Vicente Guerrero").startswith("por la salida a Ponciano")
    assert portal.pago_en_efectivo(CONOCIMIENTO, "Flores Magon").startswith("casa de Clari")
    assert portal.pago_en_efectivo(CONOCIMIENTO, "Paraíso").startswith("en Villa de Guadalupe")
    # Una zona que no aparece: el de «las demás colonias».
    assert portal.pago_en_efectivo(CONOCIMIENTO, "La Merced").startswith("en Villa de Guadalupe")
    assert portal.pago_en_efectivo("", "Paraíso") is None


def test_vender_una_ficha_en_el_sistema_de_fichas():
    from src.application.services.fichas_service import FichasService

    pedidos = []

    def responder(request: httpx.Request) -> httpx.Response:
        pedidos.append((request.method, request.url.path, request.headers.get("authorization")))
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(200, json={"access_token": "tok", "token_type": "bearer", "expires_in": 3600})
        if request.url.path.endswith("/seller/profiles"):
            return httpx.Response(200, json=[{"id": "p1", "name": "1 día", "public_price": "10.00", "session_timeout": "1d", "time_mode": "x"}])
        if request.url.path.endswith("/seller/sales/preview"):
            assert json.loads(request.content)["quantity"] == 1
            return httpx.Response(200, json={"quote_token": "q" * 24, "profile_name": "1 día", "gross_total": "10.00"})
        if request.url.path.endswith("/seller/sales"):
            assert json.loads(request.content)["quote_token"] == "q" * 24
            return httpx.Response(201, json={"folio": "F-77", "profile_name": "1 día", "gross_total": "10.00",
                                             "vouchers": [{"code": "ZN4821", "password": "ZN4821", "session_timeout": "1d"}]})
        return httpx.Response(404)

    async def probar():
        http = httpx.AsyncClient(base_url="http://fichas/api/v1", transport=httpx.MockTransport(responder))
        async with FichasService({"url": "http://fichas/api/v1", "usuario": "portal", "clave": "x"}, http) as fichas:
            perfiles = await fichas.perfiles()
            ficha = await fichas.vender("p1")
        return perfiles, ficha

    perfiles, ficha = asyncio.run(probar())
    assert perfiles == [{"id": "p1", "nombre": "1 día", "precio": 10.0, "duracion": "1d"}]
    assert (ficha["codigo"], ficha["precio"], ficha["folio"]) == ("ZN4821", 10.0, "F-77")
    # Después del token, todo va con el vendedor del portal.
    assert all(a == "Bearer tok" for _, ruta, a in pedidos if not ruta.endswith("/auth/token"))


def test_sin_vendedor_configurado_no_hay_fichas(monkeypatch):
    from src.application.services import fichas_service

    monkeypatch.delenv("FICHAS_USUARIO", raising=False)
    monkeypatch.delenv("FICHAS_CLAVE", raising=False)
    assert fichas_service.configuracion() is None
