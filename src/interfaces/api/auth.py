from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from src.infrastructure.database import get_db
from src.application.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["Autenticación"])

@router.post("/login")
async def login_access_token(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db)
):
    service = AuthService(db)
    try:
        return await service.login(form_data)
    except ValueError as e:
        # El estándar OAuth2 sugiere 401 para fallo de login
        raise HTTPException(status_code=401, detail=str(e), headers={"WWW-Authenticate": "Bearer"})