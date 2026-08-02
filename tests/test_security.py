import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from jose import JWTError

from src.application.services.user_service import UserService
from src.infrastructure import auth


def test_access_token_round_trip_has_access_type():
    token = auth.create_access_token({"sub": "operador"})
    assert auth.decode_access_token(token) == "operador"


def test_token_without_access_type_is_rejected():
    token = auth.jwt.encode(
        {"sub": "operador"},
        auth.SECRET_KEY,
        algorithm=auth.ALGORITHM,
    )
    with pytest.raises(JWTError):
        auth.decode_access_token(token)


def test_webhook_rejects_invalid_secret(monkeypatch):
    monkeypatch.setattr(auth, "WEBHOOK_SECRET", "secreto-largo-de-prueba")
    with pytest.raises(HTTPException) as error:
        asyncio.run(auth.verify_webhook_secret("otro-secreto"))
    assert error.value.status_code == 401


def test_webhook_accepts_shared_secret(monkeypatch):
    monkeypatch.setattr(auth, "WEBHOOK_SECRET", "secreto-largo-de-prueba")
    assert (
        asyncio.run(auth.verify_webhook_secret("secreto-largo-de-prueba"))
        is None
    )


def test_role_required_blocks_unlisted_role():
    dependency = auth.role_required(["admin"])
    with pytest.raises(HTTPException) as error:
        asyncio.run(dependency(SimpleNamespace(rol="tecnico")))
    assert error.value.status_code == 403


def test_role_required_normalizes_role():
    dependency = auth.role_required(["admin"])
    user = SimpleNamespace(rol=" Admin ")
    assert asyncio.run(dependency(user)) is user


def test_user_password_policy_rejects_weak_password():
    with pytest.raises(ValueError):
        UserService.validar_password("admin123")


def test_user_password_policy_accepts_letters_and_numbers():
    assert UserService.validar_password("UnaClaveSegura2026") is None


def test_login_rate_limiter_blocks_after_limit():
    limiter = auth.LoginRateLimiter(max_attempts=2, window_seconds=60)
    limiter.register_failure("ip:user")
    limiter.register_failure("ip:user")
    with pytest.raises(HTTPException) as error:
        limiter.ensure_allowed("ip:user")
    assert error.value.status_code == 429


def test_login_rate_limiter_reset_allows_login():
    limiter = auth.LoginRateLimiter(max_attempts=1, window_seconds=60)
    limiter.register_failure("ip:user")
    limiter.reset("ip:user")
    assert limiter.ensure_allowed("ip:user") is None
