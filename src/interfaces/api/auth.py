from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from src.infrastructure.database import get_db
from src.application.services.auth_service import AuthService
from src.infrastructure.auth import login_rate_limiter

router = APIRouter(prefix="/auth", tags=["Autenticación"])

@router.post("/login")
async def login_access_token(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db)
):
    client_ip = request.client.host if request.client else "unknown"
    attempt_key = f"{client_ip}:{form_data.username.strip().casefold()}"
    login_rate_limiter.ensure_allowed(attempt_key)

    service = AuthService(db)
    try:
        result = await service.login(form_data)
        login_rate_limiter.reset(attempt_key)
        return result
    except ValueError as e:
        login_rate_limiter.register_failure(attempt_key)
        # El estándar OAuth2 sugiere 401 para fallo de login
        raise HTTPException(status_code=401, detail=str(e), headers={"WWW-Authenticate": "Bearer"})
