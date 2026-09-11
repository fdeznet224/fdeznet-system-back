import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator


PREFIX = "enc:v1:"


def _cipher() -> Fernet:
    secret = os.getenv("SECRET_KEY", "").strip()
    if not secret:
        raise RuntimeError("SECRET_KEY es obligatoria para cifrar credenciales")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def encrypt_field_secret(value: str) -> str:
    if value.startswith(PREFIX):
        # Valida que el valor ya cifrado pertenezca a esta instalación.
        decrypt_field_secret(value)
        return value
    return PREFIX + _cipher().encrypt(value.encode()).decode()


def decrypt_field_secret(value: str) -> str:
    if not value.startswith(PREFIX):
        # Compatibilidad durante la ventana entre despliegue y migración.
        return value
    try:
        return _cipher().decrypt(value[len(PREFIX):].encode()).decode()
    except (InvalidToken, ValueError) as exc:
        raise RuntimeError("No se pudo descifrar una credencial de infraestructura") from exc


class EncryptedText(TypeDecorator):
    """Texto cifrado de forma transparente con la clave de la instalación."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return encrypt_field_secret(str(value))

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return decrypt_field_secret(str(value))
