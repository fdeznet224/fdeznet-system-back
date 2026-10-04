"""Usuario de sistema para lo que hace el agente de IA de WhatsApp.

Los pagos que el agente aplica con una captura y las órdenes que crea
quedaban a nombre del primer administrador, como si él los hubiera hecho.
Ahora aparecen como "Agente IA". El usuario no puede iniciar sesión: está
inactivo, su rol no es de ningún panel y su contraseña es aleatoria.
"""

import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.auth import get_password_hash
from src.infrastructure.models import UsuarioModel

USUARIO_AGENTE_IA = "Agente IA"
ROL_SISTEMA = "sistema"


async def usuario_agente_ia(db: AsyncSession) -> UsuarioModel:
    usuario = (
        await db.execute(select(UsuarioModel).where(UsuarioModel.usuario == USUARIO_AGENTE_IA))
    ).scalars().first()
    if usuario:
        return usuario
    usuario = UsuarioModel(
        usuario=USUARIO_AGENTE_IA,
        nombre_completo=USUARIO_AGENTE_IA,
        rol=ROL_SISTEMA,
        activo=False,
        password_hash=get_password_hash(secrets.token_urlsafe(32)),
    )
    db.add(usuario)
    await db.flush()
    return usuario
