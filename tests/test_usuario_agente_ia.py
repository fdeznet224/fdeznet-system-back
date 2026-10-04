"""Lo que hace el agente de IA queda a nombre de "Agente IA", no de un admin."""

import asyncio
from types import SimpleNamespace

from src.application.services.usuario_sistema_service import usuario_agente_ia
from src.infrastructure.auth import verify_password


class _Lista:
    def __init__(self, valor):
        self.valor = valor

    def scalars(self):
        return self

    def first(self):
        return self.valor


class _DB:
    def __init__(self, existente=None):
        self.existente = existente
        self.agregados = []

    async def execute(self, _consulta):
        return _Lista(self.existente)

    def add(self, objeto):
        self.agregados.append(objeto)

    async def flush(self):
        return None


def test_se_crea_un_usuario_de_sistema_que_no_puede_entrar():
    db = _DB()
    usuario = asyncio.run(usuario_agente_ia(db))
    assert usuario.usuario == "Agente IA" and usuario.nombre_completo == "Agente IA"
    assert usuario.rol == "sistema" and usuario.activo is False
    # Contraseña aleatoria: ninguna conocida sirve.
    assert not verify_password("", usuario.password_hash)
    assert not verify_password("Agente IA", usuario.password_hash)
    assert db.agregados == [usuario]


def test_si_ya_existe_se_reutiliza():
    existente = SimpleNamespace(id=40, usuario="Agente IA")
    db = _DB(existente)
    assert asyncio.run(usuario_agente_ia(db)) is existente
    assert db.agregados == []
