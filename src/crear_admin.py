"""Crea el primer administrador usando variables de entorno seguras."""
import asyncio
import os

from src.application.services.user_service import UserService
from src.domain.schemas import UsuarioCreate
from src.infrastructure.database import SessionLocal


async def create_admin():
    usuario = os.getenv("ADMIN_BOOTSTRAP_USER", "").strip()
    password = os.getenv("ADMIN_BOOTSTRAP_PASSWORD", "")
    if not usuario or not password:
        raise RuntimeError(
            "Define ADMIN_BOOTSTRAP_USER y ADMIN_BOOTSTRAP_PASSWORD "
            "temporalmente para crear el administrador"
        )

    async with SessionLocal() as db:
        service = UserService(db)
        admin = await service.crear_usuario(
            UsuarioCreate(
                nombre_completo=os.getenv(
                    "ADMIN_BOOTSTRAP_NAME",
                    "Super Administrador",
                ),
                usuario=usuario,
                password=password,
                rol="admin",
                activo=True,
                router_ids=[],
            )
        )
        print(f"Administrador '{admin.usuario}' creado correctamente")


if __name__ == "__main__":
    asyncio.run(create_admin())
