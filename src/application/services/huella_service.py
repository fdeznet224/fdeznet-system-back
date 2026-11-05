"""Entrar con la huella o la cara del celular (llaves de acceso / WebAuthn).

El usuario entra una vez con su contraseña y registra una llave en su celular;
desde ahí entra con el desbloqueo del teléfono. La huella nunca sale del
celular: aquí solo se guarda la llave pública. El reto de cada paso va en un
token firmado que dura 5 minutos, así no hay que guardarlo en el servidor.
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import jwt
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.exceptions import InvalidAuthenticationResponse, InvalidRegistrationResponse
from webauthn.helpers.structs import (
    AuthenticatorAttachment,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from src.application.services.auth_service import AuthService
from src.infrastructure.auth import ALGORITHM, SECRET_KEY
from src.infrastructure.models import ConfiguracionSistema, CredencialAccesoModel, UsuarioModel

VIGENCIA_RETO = timedelta(minutes=5)
MAX_LLAVES = 5
ROLES_CON_HUELLA = ("admin", "supervisor", "cajero", "tecnico")


def sitio(origen_peticion: Optional[str] = None) -> tuple[str, str]:
    """(rp_id, origen): el dominio del sistema. Sin PUBLIC_URL (desarrollo), el de la petición."""
    base = (os.getenv("PUBLIC_URL", "").strip() or origen_peticion or "").rstrip("/")
    partes = urlparse(base)
    if not partes.scheme or not partes.hostname:
        raise ValueError("Falta la dirección del sistema (PUBLIC_URL) para usar la huella.")
    rp_id = os.getenv("WEBAUTHN_RP_ID", "").strip() or partes.hostname
    return rp_id, f"{partes.scheme}://{partes.netloc}"


def _token(proposito: str, reto: bytes, usuario_id: Optional[int] = None) -> str:
    return jwt.encode(
        {"proposito": proposito, "reto": bytes_to_base64url(reto), "uid": usuario_id,
         "exp": datetime.now(timezone.utc) + VIGENCIA_RETO},
        SECRET_KEY, algorithm=ALGORITHM,
    )


def _leer_token(token: str, proposito: str) -> Dict[str, Any]:
    try:
        datos = jwt.decode(token or "", SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.PyJWTError as error:
        raise ValueError("Se venció; vuelve a intentarlo.") from error
    if datos.get("proposito") != proposito:
        raise ValueError("Se venció; vuelve a intentarlo.")
    return datos


class HuellaService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def _nombre_sistema(self) -> str:
        marca = await self.db.get(ConfiguracionSistema, 1)
        return getattr(marca, "sistema_nombre", None) or getattr(marca, "empresa_nombre", None) or "Sistema"

    async def llaves(self, usuario_id: int) -> List[CredencialAccesoModel]:
        return (await self.db.execute(
            select(CredencialAccesoModel).where(CredencialAccesoModel.usuario_id == usuario_id)
            .order_by(CredencialAccesoModel.id)
        )).scalars().all()

    async def _llave(self, credencial_id: str) -> Optional[CredencialAccesoModel]:
        return (await self.db.execute(
            select(CredencialAccesoModel).where(CredencialAccesoModel.credencial_id == credencial_id)
        )).scalars().first()

    # ------------------------------------------------------------------ registrar
    async def opciones_registro(self, usuario: UsuarioModel, origen: Optional[str] = None) -> Dict[str, Any]:
        if usuario.rol not in ROLES_CON_HUELLA:
            raise PermissionError("Tu usuario no puede entrar con huella.")
        rp_id, _ = sitio(origen)
        existentes = await self.llaves(usuario.id)
        if len(existentes) >= MAX_LLAVES:
            raise ValueError(f"Ya tienes {MAX_LLAVES} celulares con huella; quita uno antes.")
        opciones = generate_registration_options(
            rp_id=rp_id,
            rp_name=await self._nombre_sistema(),
            user_name=usuario.usuario,
            user_id=str(usuario.id).encode(),
            user_display_name=usuario.nombre_completo or usuario.usuario,
            authenticator_selection=AuthenticatorSelectionCriteria(
                # La llave vive en el celular y se pide la huella o la cara (no solo tocar).
                authenticator_attachment=AuthenticatorAttachment.PLATFORM,
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(c.credencial_id)) for c in existentes],
        )
        return {"opciones": options_to_json(opciones), "token": _token("registro", opciones.challenge, usuario.id)}

    async def registrar(self, usuario: UsuarioModel, token: str, credencial: Dict[str, Any],
                        nombre: Optional[str] = None, origen: Optional[str] = None) -> CredencialAccesoModel:
        datos = _leer_token(token, "registro")
        if datos.get("uid") != usuario.id:
            raise ValueError("Se venció; vuelve a intentarlo.")
        rp_id, esperado = sitio(origen)
        try:
            verificada = verify_registration_response(
                credential=credencial, expected_challenge=base64url_to_bytes(datos["reto"]),
                expected_rp_id=rp_id, expected_origin=esperado, require_user_verification=True,
            )
        except InvalidRegistrationResponse as error:
            raise ValueError("El celular no confirmó la huella; intenta de nuevo.") from error
        llave = CredencialAccesoModel(
            usuario_id=usuario.id,
            credencial_id=bytes_to_base64url(verificada.credential_id),
            llave_publica=verificada.credential_public_key,
            contador=verificada.sign_count,
            nombre=(nombre or "").strip()[:80] or None,
        )
        self.db.add(llave)
        await self.db.commit()
        return llave

    # ------------------------------------------------------------------ entrar
    def opciones_entrada(self, origen: Optional[str] = None) -> Dict[str, Any]:
        rp_id, _ = sitio(origen)
        # Sin lista de llaves: el celular ofrece la que tenga para este sistema (no hay que escribir el usuario).
        opciones = generate_authentication_options(rp_id=rp_id, user_verification=UserVerificationRequirement.REQUIRED)
        return {"opciones": options_to_json(opciones), "token": _token("entrada", opciones.challenge)}

    async def entrar(self, token: str, credencial: Dict[str, Any], origen: Optional[str] = None) -> Dict[str, Any]:
        datos = _leer_token(token, "entrada")
        llave = await self._llave(str(credencial.get("id") or ""))
        usuario = await self.db.get(UsuarioModel, llave.usuario_id) if llave else None
        if not llave or not usuario or not usuario.activo or usuario.rol not in ROLES_CON_HUELLA:
            raise ValueError("Esa huella ya no está registrada; entra con tu contraseña.")
        rp_id, esperado = sitio(origen)
        try:
            verificada = verify_authentication_response(
                credential=credencial, expected_challenge=base64url_to_bytes(datos["reto"]),
                expected_rp_id=rp_id, expected_origin=esperado,
                credential_public_key=llave.llave_publica, credential_current_sign_count=llave.contador,
                require_user_verification=True,
            )
        except InvalidAuthenticationResponse as error:
            raise ValueError("No se pudo confirmar la huella; entra con tu contraseña.") from error
        llave.contador = verificada.new_sign_count
        llave.ultimo_uso = datetime.now()
        await self.db.commit()
        return AuthService.sesion_para(usuario)

    # ------------------------------------------------------------------ quitar
    async def quitar(self, usuario_id: int, llave_id: Optional[int] = None) -> int:
        """Una llave del usuario, o todas (el admin, si alguien perdió su celular)."""
        consulta = delete(CredencialAccesoModel).where(CredencialAccesoModel.usuario_id == usuario_id)
        if llave_id is not None:
            consulta = consulta.where(CredencialAccesoModel.id == llave_id)
        resultado = await self.db.execute(consulta)
        await self.db.commit()
        return resultado.rowcount or 0
