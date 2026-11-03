from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from src.infrastructure.database import get_db
from src.application.services.auth_service import AuthService
from src.application.services.huella_service import HuellaService
from src.infrastructure.auth import get_current_user, login_rate_limiter

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
        _guardar_sesion(request, response, result)
        login_rate_limiter.reset(attempt_key)
        return result
    except ValueError as e:
        login_rate_limiter.register_failure(attempt_key)
        # El estándar OAuth2 sugiere 401 para fallo de login
        raise HTTPException(status_code=401, detail=str(e), headers={"WWW-Authenticate": "Bearer"})


def _guardar_sesion(request: Request, response: Response, result: dict) -> None:
    forwarded_scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    response.set_cookie(
        key="fdeznet_access",
        value=result["access_token"],
        # Igual que el token: los técnicos tienen sesión de jornada.
        max_age=int(result["expira_en_minutos"]) * 60,
        httponly=True,
        secure=forwarded_scheme == "https",
        samesite="strict",
        path="/",
    )


def _origen(request: Request) -> Optional[str]:
    """Solo en desarrollo (sin PUBLIC_URL) se toma el dominio de la petición."""
    return request.headers.get("origin")


# --- Entrar con huella o cara (llaves de acceso) -------------------------

class RegistroHuella(BaseModel):
    token: str = Field(max_length=2000)
    credencial: Dict[str, Any]
    nombre: Optional[str] = Field(default=None, max_length=80)


class EntradaHuella(BaseModel):
    token: str = Field(max_length=2000)
    credencial: Dict[str, Any]


def _error_huella(error: Exception) -> HTTPException:
    if isinstance(error, PermissionError):
        return HTTPException(status_code=403, detail=str(error))
    return HTTPException(status_code=400, detail=str(error))


@router.get("/huella")
async def mis_huellas(db: AsyncSession = Depends(get_db), usuario=Depends(get_current_user)):
    llaves = await HuellaService(db).llaves(usuario.id)
    return [{"id": l.id, "nombre": l.nombre, "creado": l.creado, "ultimo_uso": l.ultimo_uso} for l in llaves]


@router.post("/huella/registro/opciones")
async def opciones_registro_huella(request: Request, db: AsyncSession = Depends(get_db),
                                   usuario=Depends(get_current_user)):
    try:
        return await HuellaService(db).opciones_registro(usuario, _origen(request))
    except (ValueError, PermissionError) as error:
        raise _error_huella(error) from error


@router.post("/huella/registro", status_code=201)
async def registrar_huella(datos: RegistroHuella, request: Request, db: AsyncSession = Depends(get_db),
                           usuario=Depends(get_current_user)):
    try:
        llave = await HuellaService(db).registrar(usuario, datos.token, datos.credencial, datos.nombre, _origen(request))
    except (ValueError, PermissionError) as error:
        raise _error_huella(error) from error
    return {"id": llave.id, "nombre": llave.nombre}


@router.delete("/huella/{llave_id}", status_code=204)
async def quitar_huella(llave_id: int, db: AsyncSession = Depends(get_db), usuario=Depends(get_current_user)):
    await HuellaService(db).quitar(usuario.id, llave_id)


@router.post("/huella/entrar/opciones")
async def opciones_entrar_huella(request: Request, db: AsyncSession = Depends(get_db)):
    try:
        return HuellaService(db).opciones_entrada(_origen(request))
    except ValueError as error:
        raise _error_huella(error) from error


@router.post("/huella/entrar")
async def entrar_con_huella(datos: EntradaHuella, request: Request, response: Response,
                            db: AsyncSession = Depends(get_db)):
    client_ip = request.client.host if request.client else "unknown"
    attempt_key = f"{client_ip}:huella"
    login_rate_limiter.ensure_allowed(attempt_key)
    try:
        result = await HuellaService(db).entrar(datos.token, datos.credencial, _origen(request))
    except ValueError as error:
        login_rate_limiter.register_failure(attempt_key)
        raise HTTPException(status_code=401, detail=str(error)) from error
    _guardar_sesion(request, response, result)
    login_rate_limiter.reset(attempt_key)
    return result


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
