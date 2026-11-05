"""Entrar con la huella: registro y entrada con un autenticador de software (llave P-256 real)."""
import asyncio
import hashlib
import json
import os
from types import SimpleNamespace

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url

from src.application.services import huella_service
from src.application.services.huella_service import HuellaService

ORIGEN = "https://fdezpay.com"


class Celular:
    """Hace lo que el celular: guarda la llave privada y firma con la huella (UV)."""

    def __init__(self, rp_id="fdezpay.com"):
        self.privada = ec.generate_private_key(ec.SECP256R1())
        self.id = os.urandom(16)
        self.rp_hash = hashlib.sha256(rp_id.encode()).digest()
        self.contador = 0

    def _cliente(self, tipo, reto, origen):
        return json.dumps({"type": tipo, "challenge": reto, "origin": origen, "crossOrigin": False}).encode()

    def crear(self, opciones, origen=ORIGEN):
        publica = self.privada.public_key().public_numbers()
        cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: publica.x.to_bytes(32, "big"), -3: publica.y.to_bytes(32, "big")})
        datos = (self.rp_hash + bytes([0x45]) + self.contador.to_bytes(4, "big")
                 + bytes(16) + len(self.id).to_bytes(2, "big") + self.id + cose)
        cliente = self._cliente("webauthn.create", opciones["challenge"], origen)
        objeto = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": datos})
        return {"id": bytes_to_base64url(self.id), "rawId": bytes_to_base64url(self.id), "type": "public-key",
                "response": {"clientDataJSON": bytes_to_base64url(cliente), "attestationObject": bytes_to_base64url(objeto)}}

    def firmar(self, opciones, origen=ORIGEN, huella=True):
        self.contador += 1
        datos = self.rp_hash + bytes([0x05 if huella else 0x01]) + self.contador.to_bytes(4, "big")
        cliente = self._cliente("webauthn.get", opciones["challenge"], origen)
        firma = self.privada.sign(datos + hashlib.sha256(cliente).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": bytes_to_base64url(self.id), "rawId": bytes_to_base64url(self.id), "type": "public-key",
                "response": {"clientDataJSON": bytes_to_base64url(cliente), "authenticatorData": bytes_to_base64url(datos),
                             "signature": bytes_to_base64url(firma), "userHandle": bytes_to_base64url(b"7")}}


class _Db:
    def __init__(self, usuario):
        self.usuario, self.guardadas = usuario, []

    async def get(self, modelo, ident):
        return self.usuario if modelo.__name__ == "UsuarioModel" and ident == self.usuario.id else None

    def add(self, objeto):
        self.guardadas.append(objeto)

    async def commit(self):
        pass


@pytest.fixture
def servicio(monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", ORIGEN)
    usuario = SimpleNamespace(id=7, usuario="luis", nombre_completo="Luis Técnico", rol="tecnico", activo=True)
    db = _Db(usuario)
    servicio = HuellaService(db)

    async def llaves(_uid):
        return db.guardadas

    async def llave(credencial_id):
        return next((l for l in db.guardadas if l.credencial_id == credencial_id), None)

    async def nombre():
        return "FdezNet"

    monkeypatch.setattr(servicio, "llaves", llaves)
    monkeypatch.setattr(servicio, "_llave", llave)
    monkeypatch.setattr(servicio, "_nombre_sistema", nombre)
    return servicio, usuario, db


def _registrar(servicio, usuario, celular, nombre="Celular de Luis"):
    paso = asyncio.run(servicio.opciones_registro(usuario))
    opciones = json.loads(paso["opciones"])
    assert opciones["rp"]["id"] == "fdezpay.com"
    assert opciones["authenticatorSelection"]["userVerification"] == "required"
    return asyncio.run(servicio.registrar(usuario, paso["token"], celular.crear(opciones), nombre))


def test_registra_la_huella_y_entra_con_ella(servicio):
    servicio, usuario, db = servicio
    celular = Celular()
    llave = _registrar(servicio, usuario, celular)
    assert llave.nombre == "Celular de Luis" and llave.credencial_id == bytes_to_base64url(celular.id)

    paso = servicio.opciones_entrada()
    sesion = asyncio.run(servicio.entrar(paso["token"], celular.firmar(json.loads(paso["opciones"]))))
    assert sesion["user"] == {"id": 7, "usuario": "luis", "rol": "tecnico"} and sesion["access_token"]
    assert llave.contador == 1 and llave.ultimo_uso is not None


def test_no_entra_desde_otra_pagina_ni_sin_huella_ni_dos_veces(servicio):
    servicio, usuario, _ = servicio
    celular = Celular()
    _registrar(servicio, usuario, celular)

    paso = servicio.opciones_entrada()
    opciones = json.loads(paso["opciones"])
    with pytest.raises(ValueError):  # una página falsa que pide la huella
        asyncio.run(servicio.entrar(paso["token"], celular.firmar(opciones, origen="https://fdezpay-falso.com")))
    with pytest.raises(ValueError):  # solo tocó el sensor, sin huella ni cara
        asyncio.run(servicio.entrar(paso["token"], celular.firmar(opciones, huella=False)))
    otro = Celular()  # un celular que nunca registró
    with pytest.raises(ValueError, match="ya no está registrada"):
        asyncio.run(servicio.entrar(paso["token"], otro.firmar(opciones)))
    with pytest.raises(ValueError, match="venció"):
        asyncio.run(servicio.entrar("token-inventado", celular.firmar(opciones)))


def test_usuario_desactivado_ya_no_entra_con_su_huella(servicio):
    servicio, usuario, _ = servicio
    celular = Celular()
    _registrar(servicio, usuario, celular)
    usuario.activo = False
    paso = servicio.opciones_entrada()
    with pytest.raises(ValueError):
        asyncio.run(servicio.entrar(paso["token"], celular.firmar(json.loads(paso["opciones"]))))


def test_el_reto_de_registro_no_sirve_para_otro_usuario(servicio):
    servicio, usuario, _ = servicio
    paso = asyncio.run(servicio.opciones_registro(usuario))
    otro = SimpleNamespace(id=8, usuario="ana", nombre_completo="Ana", rol="cajero", activo=True)
    with pytest.raises(ValueError):
        asyncio.run(servicio.registrar(otro, paso["token"], Celular().crear(json.loads(paso["opciones"]))))


def test_dominio_de_la_llave_sale_de_public_url(monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "https://prueba.wispay.click/")
    monkeypatch.delenv("WEBAUTHN_RP_ID", raising=False)
    assert huella_service.sitio() == ("prueba.wispay.click", "https://prueba.wispay.click")
    monkeypatch.delenv("PUBLIC_URL")
    assert huella_service.sitio("http://localhost:5173") == ("localhost", "http://localhost:5173")
    assert base64url_to_bytes(bytes_to_base64url(b"x")) == b"x"
