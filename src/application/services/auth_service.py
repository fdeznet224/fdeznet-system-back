from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from fastapi.security import OAuth2PasswordRequestForm
from src.infrastructure.models import UsuarioModel
from src.infrastructure.auth import verify_password, create_access_token # Verifica que el import sea correcto

class AuthService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def login(self, form_data: OAuth2PasswordRequestForm):
        # 1. Buscar usuario
        stmt = select(UsuarioModel).where(UsuarioModel.usuario == form_data.username)
        result = await self.db.execute(stmt)
        user = result.scalar()

        # 2. Validar
        if not user or not verify_password(form_data.password, user.password_hash):
            raise ValueError("Usuario o contraseña incorrectos")
        
        if not user.activo:
            raise ValueError("Usuario inactivo")

        # 3. Generar Token
        access_token = create_access_token(data={"sub": user.usuario, "rol": user.rol})
        
        # 4. RETORNAR EL OBJETO COMPLETO (Lo que tu Front espera)
        return {
            "access_token": access_token, 
            "token_type": "bearer",
            "user": {
                "id": user.id,
                "usuario": user.usuario,
                "rol": user.rol
            }
        }