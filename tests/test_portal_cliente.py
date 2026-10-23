"""Portal del cliente: entra con su contrato, pero hasta poner su propia contraseña no ve nada."""

import asyncio
from types import SimpleNamespace

import jwt
import pytest
from fastapi import HTTPException

from src.application.services import portal_cliente_service as portal
from src.application.services.portal_cliente_service import PortalClienteService, crear_token, leer_token, validar_clave_nueva
from src.infrastructure import auth
from src.infrastructure.models import ClienteModel, PortalAccesoModel
from src.interfaces.api import portal_cliente as rutas


class BaseFalsa:
    def __init__(self, clientes):
        self.clientes = {c.id: c for c in clientes}
        self.accesos = {}

    async def get(self, modelo, llave):
        return self.clientes.get(llave) if modelo is ClienteModel else self.accesos.get(llave)

    def add(self, objeto):
        self.accesos[objeto.cliente_id] = objeto

    async def delete(self, objeto):
        self.accesos.pop(objeto.cliente_id, None)

    async def commit(self):
        pass


def servicio_con(cliente):
    db = BaseFalsa([cliente])
    servicio = PortalClienteService(db)

    async def por_contrato(contrato):
        return cliente if portal.normalizar_contrato(contrato) == cliente.cedula else None

    servicio._cliente_por_contrato = por_contrato
    return servicio, db


def cliente(**extra):
    return SimpleNamespace(id=327, cedula="F1C8", nombre="Casa", estado="activo", **extra)


@pytest.fixture(autouse=True)
def bcrypt_rapido(monkeypatch):
    monkeypatch.setattr(auth, "get_password_hash", lambda clave: f"hash:{clave}")
    monkeypatch.setattr(auth, "verify_password", lambda clave, guardado: guardado == f"hash:{clave}")


def test_primera_vez_entra_con_el_contrato_pero_debe_cambiarla():
    servicio, _ = servicio_con(cliente())
    # La O del cliente se lee como 0, como en el bot.
    resultado = asyncio.run(servicio.entrar("f1c8", "f1c8"))
    assert resultado["debe_cambiar"] is True
    assert leer_token(resultado["token"]) == {"cliente_id": 327, "version": 0, "cambiar": True}
    with pytest.raises(ValueError, match="incorrectos"):
        asyncio.run(servicio.entrar("F1C8", "otra"))
    with pytest.raises(ValueError, match="incorrectos"):
        asyncio.run(servicio.entrar("AAAA", "AAAA"))


def test_con_contraseña_propia_el_contrato_ya_no_sirve_y_las_sesiones_viejas_se_cierran():
    c = cliente()
    servicio, db = servicio_con(c)
    vieja = asyncio.run(servicio.entrar("F1C8", "F1C8"))["token"]
    nueva = asyncio.run(servicio.cambiar_clave(c, "F1C8", "mi-clave-segura"))
    assert db.accesos[327].version == 1
    with pytest.raises(ValueError):
        asyncio.run(servicio.entrar("F1C8", "F1C8"))
    assert asyncio.run(servicio.entrar("F1C8", "mi-clave-segura"))["debe_cambiar"] is False
    with pytest.raises(PermissionError):
        asyncio.run(servicio.sesion(vieja))
    assert asyncio.run(servicio.sesion(nueva["token"]))["debe_cambiar"] is False

    # Olvidó la contraseña: el personal la restablece y vuelve a ser el contrato.
    asyncio.run(servicio.restablecer(327))
    with pytest.raises(PermissionError):
        asyncio.run(servicio.sesion(nueva["token"]))
    assert asyncio.run(servicio.entrar("F1C8", "F1C8"))["debe_cambiar"] is True


def test_la_contraseña_nueva_no_puede_ser_el_contrato_que_esta_en_el_wifi():
    for mala in ("F1C8", "corta", "casaF1C8casa", "x" * 65):
        with pytest.raises(ValueError):
            validar_clave_nueva(mala, "F1C8")
    validar_clave_nueva("mi-clave-segura", "F1C8")


def test_cambiar_pide_la_actual():
    c = cliente()
    servicio, _ = servicio_con(c)
    with pytest.raises(ValueError, match="actual"):
        asyncio.run(servicio.cambiar_clave(c, "otra", "mi-clave-segura"))


def test_tokens_del_personal_y_del_portal_no_se_cruzan():
    token_portal = crear_token(327, 1, False)
    with pytest.raises(jwt.PyJWTError):
        auth.decode_access_token(token_portal)
    token_personal = auth.create_access_token({"sub": "admin"})
    with pytest.raises(jwt.PyJWTError):
        leer_token(token_personal)


def test_cliente_eliminado_no_entra():
    servicio, _ = servicio_con(cliente())
    servicio.db.clientes[327].estado = "eliminado"
    token = crear_token(327, 0, True)
    with pytest.raises(PermissionError):
        asyncio.run(servicio.sesion(token))


def test_sin_cambiar_la_contraseña_no_ve_nada():
    with pytest.raises(HTTPException) as error:
        asyncio.run(rutas.cliente_portal({"cliente": cliente(), "debe_cambiar": True}))
    assert error.value.status_code == 403
    assert asyncio.run(rutas.cliente_portal({"cliente": cliente(), "debe_cambiar": False})).id == 327


def test_limite_de_intentos_por_contrato():
    limitador = rutas.LoginRateLimiter(max_attempts=5, window_seconds=900)
    for _ in range(5):
        limitador.ensure_allowed("F1C8")
        limitador.register_failure("F1C8")
    with pytest.raises(HTTPException) as error:
        limitador.ensure_allowed("F1C8")
    assert error.value.status_code == 429


def test_acceso_se_guarda_con_hash_y_version():
    c = cliente()
    servicio, db = servicio_con(c)
    asyncio.run(servicio.cambiar_clave(c, "F1C8", "mi-clave-segura"))
    acceso = db.accesos[327]
    assert isinstance(acceso, PortalAccesoModel)
    assert acceso.password_hash == "hash:mi-clave-segura" and acceso.version == 1
