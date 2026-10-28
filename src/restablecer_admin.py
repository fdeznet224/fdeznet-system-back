"""Pone una contraseña nueva al administrador de la instancia (panel de la nube).

La contraseña llega por la variable RESTABLECER_PASSWORD, nunca por argumentos.
Si el usuario no existe se crea como administrador; si estaba inactivo, se activa.
"""
import asyncio
import os

from sqlalchemy import select

from src.application.services.user_service import UserService
from src.domain.schemas import UsuarioCreate
from src.infrastructure.database import SessionLocal
from src.infrastructure.models import UsuarioModel


async def restablecer() -> None:
    usuario = os.getenv("RESTABLECER_USUARIO", "admin").strip()
    password = os.getenv("RESTABLECER_PASSWORD", "")
    if not usuario or len(password) < 12:
        raise RuntimeError("Define RESTABLECER_PASSWORD (12+ caracteres)")

    async with SessionLocal() as db:
        servicio = UserService(db)
        admin = await db.scalar(select(UsuarioModel).where(UsuarioModel.usuario == usuario))
        if admin is None:
            await servicio.crear_usuario(UsuarioCreate(
                nombre_completo="Super Administrador", usuario=usuario, password=password,
                rol="admin", activo=True, router_ids=[],
            ))
            print(f"RESTABLECIDO={usuario} (creado)")
            return
        admin.password_hash = servicio.get_password_hash(password)
        admin.activo = True
        await db.commit()
        print(f"RESTABLECIDO={usuario}")


if __name__ == "__main__":
    asyncio.run(restablecer())
