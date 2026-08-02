import os
import secrets
import threading
import time
import warnings
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional
from dotenv import load_dotenv
from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from src.infrastructure.database import get_db
from src.infrastructure.models import UsuarioModel

# Cargar variables del .env
load_dotenv()

ENVIRONMENT = os.getenv("ENVIRONMENT", "development").strip().lower()
SECRET_KEY = os.getenv("SECRET_KEY", "").strip()
ALGORITHM = os.getenv("ALGORITHM", "HS256").strip().upper()
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
LOGIN_MAX_ATTEMPTS = int(os.getenv("LOGIN_MAX_ATTEMPTS", "5"))
LOGIN_WINDOW_SECONDS = int(os.getenv("LOGIN_WINDOW_SECONDS", "900"))

if ALGORITHM not in {"HS256", "HS384", "HS512"}:
    raise RuntimeError("ALGORITHM debe ser HS256, HS384 o HS512")

if not SECRET_KEY:
    if ENVIRONMENT == "production":
        raise RuntimeError("SECRET_KEY es obligatoria en producción")
    SECRET_KEY = secrets.token_urlsafe(48)
    warnings.warn(
        "SECRET_KEY no está configurada; se generó una clave temporal de desarrollo",
        RuntimeWarning,
        stacklevel=2,
    )

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


class LoginRateLimiter:
    """Limitador local por IP y usuario para frenar fuerza bruta."""

    def __init__(self, max_attempts: int, window_seconds: int):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._attempts = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, attempts, now):
        cutoff = now - self.window_seconds
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()

    def ensure_allowed(self, key: str):
        now = time.monotonic()
        with self._lock:
            attempts = self._attempts[key]
            self._prune(attempts, now)
            if len(attempts) >= self.max_attempts:
                retry_after = max(1, int(self.window_seconds - (now - attempts[0])))
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Demasiados intentos de acceso. Intenta más tarde",
                    headers={"Retry-After": str(retry_after)},
                )

    def register_failure(self, key: str):
        now = time.monotonic()
        with self._lock:
            attempts = self._attempts[key]
            self._prune(attempts, now)
            attempts.append(now)

    def reset(self, key: str):
        with self._lock:
            self._attempts.pop(key, None)


login_rate_limiter = LoginRateLimiter(
    max_attempts=LOGIN_MAX_ATTEMPTS,
    window_seconds=LOGIN_WINDOW_SECONDS,
)

# --- UTILIDADES ---

def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    now = datetime.now(timezone.utc)
    if expires_delta:
        expire = now + expires_delta
    else:
        expire = now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    
    to_encode.update({
        "exp": expire,
        "iat": now,
        "jti": secrets.token_urlsafe(16),
        "type": "access",
    })
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def decode_access_token(token: str) -> str:
    payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    username = payload.get("sub")
    if not username or payload.get("type") != "access":
        raise JWTError("Token de acceso inválido")
    return username

# --- DEPENDENCIAS ---

async def get_current_user(
    request: Request,
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db)
) -> UsuarioModel:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="No se pudo validar las credenciales",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        username = decode_access_token(token)
    except JWTError:
        raise credentials_exception

    result = await db.execute(select(UsuarioModel).where(UsuarioModel.usuario == username))
    user = result.scalar_one_or_none()
    
    if user is None:
        raise credentials_exception

    request.state.current_user = user
    return user

async def get_current_active_user(
    current_user: UsuarioModel = Depends(get_current_user)
) -> UsuarioModel:
    if not current_user.activo:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Usuario inactivo")
    return current_user

def role_required(allowed_roles: Iterable[str]):
    roles_permitidos = {role.strip().lower() for role in allowed_roles}

    async def decorator(current_user: UsuarioModel = Depends(get_current_active_user)):
        rol_usuario = current_user.rol.strip().lower() if current_user.rol else ""

        if rol_usuario not in roles_permitidos:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes permisos suficientes para realizar esta acción"
            )
        return current_user
    return decorator


async def verify_webhook_secret(
    x_webhook_secret: Optional[str] = Header(default=None),
) -> None:
    """Valida el secreto compartido del puente de WhatsApp."""
    if not WEBHOOK_SECRET:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook no configurado",
        )
    if not x_webhook_secret or not secrets.compare_digest(
        x_webhook_secret,
        WEBHOOK_SECRET,
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Firma de webhook inválida",
        )
