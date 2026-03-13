import asyncio
from passlib.context import CryptContext
from sqlalchemy import select
from src.infrastructure.database import async_session_factory
from src.infrastructure.models import UsuarioModel

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

async def create_admin():
    async with async_session_factory() as db:
        # Verificar si existe
        result = await db.execute(select(UsuarioModel).where(UsuarioModel.usuario == "admin"))
        if result.scalar():
            print("⚠️ El usuario admin ya existe")
            return

        nuevo_admin = UsuarioModel(
            nombre_completo="Administrador",
            usuario="admin",
            password_hash=pwd_context.hash("admin123"), # Tu contraseña aquí
            rol="admin",
            activo=True
        )
        db.add(nuevo_admin)
        await db.commit()
        print("✅ Usuario 'admin' creado con contraseña 'admin123'")

if __name__ == "__main__":
    asyncio.run(create_admin())