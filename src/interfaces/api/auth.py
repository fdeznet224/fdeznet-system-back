import os

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from src.infrastructure.database import get_db
from src.application.services.auth_service import AuthService
from src.infrastructure.auth import login_rate_limiter

router = APIRouter(prefix="/auth", tags=["Autenticación"])

@router.post("/login")
async def login_access_token(
    request: Request,
    response: Response,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db)
):
    client_ip = request.client.host if request.client else "unknown"
    attempt_key = f"{client_ip}:{form_data.username.strip().casefold()}"
    login_rate_limiter.ensure_allowed(attempt_key)

    service = AuthService(db)
    try:
        result = await service.login(form_data)
        forwarded_scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
        response.set_cookie(
            key="fdeznet_access",
            value=result["access_token"],
            max_age=int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60")) * 60,
            httponly=True,
            secure=forwarded_scheme == "https",
            samesite="strict",
            path="/",
        )
        login_rate_limiter.reset(attempt_key)
        return result
    except ValueError as e:
        login_rate_limiter.register_failure(attempt_key)
        # El estándar OAuth2 sugiere 401 para fallo de login
        raise HTTPException(status_code=401, detail=str(e), headers={"WWW-Authenticate": "Bearer"})


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response):
    forwarded_scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    response.delete_cookie(
        "fdeznet_access",
        path="/",
        httponly=True,
        secure=forwarded_scheme == "https",
        samesite="strict",
    )
