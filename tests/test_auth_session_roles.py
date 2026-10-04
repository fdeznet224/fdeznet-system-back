"""Los técnicos tienen una sesión de jornada; el resto conserva la corta."""

import asyncio
from types import SimpleNamespace

import jwt

from src.application.services.auth_service import AuthService
from src.infrastructure import auth


class _DB:
    def __init__(self, usuario):
        self.usuario = usuario

    async def execute(self, _consulta):
        return SimpleNamespace(scalar=lambda: self.usuario)


def _login(rol):
    usuario = SimpleNamespace(id=2, usuario="TEC", rol=rol, activo=True, password_hash=auth.get_password_hash("x"))
    resultado = asyncio.run(AuthService(_DB(usuario)).login(SimpleNamespace(username="TEC", password="x")))
    datos = jwt.decode(resultado["access_token"], auth.SECRET_KEY, algorithms=[auth.ALGORITHM])
    return resultado["expira_en_minutos"], (datos["exp"] - datos["iat"]) / 60


def test_el_tecnico_tiene_sesion_de_doce_horas():
    minutos, duracion = _login("tecnico")
    assert minutos == 720 and round(duracion) == 720


def test_admin_y_cajero_conservan_la_sesion_corta():
    for rol in ("admin", "cajero", "supervisor"):
        minutos, duracion = _login(rol)
        assert minutos == auth.ACCESS_TOKEN_EXPIRE_MINUTES and round(duracion) == minutos
