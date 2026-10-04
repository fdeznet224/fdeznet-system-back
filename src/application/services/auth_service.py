from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from fastapi.security import OAuth2PasswordRequestForm
from src.infrastructure.models import UsuarioModel
from datetime import timedelta

from src.infrastructure.auth import create_access_token, minutos_de_sesion, verify_password

class AuthService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def login(self, form_data: OAuth2PasswordRequestForm):
        # 1. Buscar usuario
        stmt = select(UsuarioModel).where(UsuarioModel.usuario == form_data.username)
        result = await self.db.execute(stmt)
        user = result.scalar()

        # 2. Validar
        if (
            not user
            or not verify_password(form_data.password, user.password_hash)
            or not user.activo
        ):
            raise ValueError("Usuario o contraseña incorrectos")

        # 3. Generar Token
        minutos = minutos_de_sesion(user.rol)
        access_token = create_access_token(
            data={"sub": user.usuario, "rol": user.rol}, expires_delta=timedelta(minutes=minutos)
        )
        
        # 4. RETORNAR EL OBJETO COMPLETO (Lo que tu Front espera)
        return {
            "access_token": access_token, 
            "token_type": "bearer",
            "expira_en_minutos": minutos,
            "user": {
                "id": user.id,
                "usuario": user.usuario,
                "rol": user.rol
            }
        }
