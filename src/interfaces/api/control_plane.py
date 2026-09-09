import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from hmac import compare_digest
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.services.license_service import update_available
from src.domain.schemas import (
    InstallationCreate,
    InstallationCreated,
    InstallationResponse,
    InstallationUpdate,
    BootstrapExchangeRequest,
    BootstrapExchangeResponse,
    BootstrapTokenResponse,
    LicenseHeartbeatRequest,
    LicenseHeartbeatResponse,
)
from src.infrastructure.database import get_db
from src.infrastructure.models import InstalacionSistemaModel


heartbeat_router = APIRouter(prefix="/control", tags=["Control de instalaciones"])
router = APIRouter(prefix="/control", tags=["Control de instalaciones"])
CONTROL_PLANE_MODE = os.getenv("FDEZNET_CONTROL_PLANE_MODE", "client").strip().lower()
CONTROL_PUBLIC_URL = os.getenv("FDEZNET_CONTROL_URL", "https://fdezpay.com/api").rstrip("/")
INSTALLER_PATH = Path(__file__).resolve().parents[3] / "install.sh"


def _ensure_central() -> None:
    if CONTROL_PLANE_MODE != "central":
        raise HTTPException(status_code=404, detail="Control central no disponible")


def _hash_license(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _assign_bootstrap(installation: InstalacionSistemaModel) -> tuple[str, datetime]:
    token = f"fdz_setup_{secrets.token_urlsafe(32)}"
    expires = _now() + timedelta(hours=48)
    installation.bootstrap_hash = _hash_license(token)
    installation.bootstrap_expira = expires
    installation.bootstrap_usado_en = None
    return token, expires


@heartbeat_router.get("/installer", response_class=FileResponse)
async def download_installer():
    _ensure_central()
    if not INSTALLER_PATH.is_file():
        raise HTTPException(status_code=404, detail="Instalador no disponible")
    return FileResponse(
        INSTALLER_PATH,
        media_type="text/x-shellscript",
        filename="fdeznet-install.sh",
    )


@heartbeat_router.post("/bootstrap", response_model=BootstrapExchangeResponse)
async def exchange_bootstrap(
    payload: BootstrapExchangeRequest,
    db: AsyncSession = Depends(get_db),
):
    _ensure_central()
    token_hash = _hash_license(payload.token)
    installation = (
        await db.execute(
            select(InstalacionSistemaModel)
            .where(InstalacionSistemaModel.bootstrap_hash == token_hash)
            .with_for_update()
        )
    ).scalar_one_or_none()
    now = _now()
    if (
        installation is None
        or installation.bootstrap_usado_en is not None
        or installation.bootstrap_expira is None
        or installation.bootstrap_expira < now
        or installation.estado != "activa"
    ):
        raise HTTPException(status_code=401, detail="Token de instalación inválido o vencido")

    raw_license = f"fdz_live_{secrets.token_urlsafe(32)}"
    installation.licencia_hash = _hash_license(raw_license)
    installation.bootstrap_usado_en = now
    await db.commit()
    return BootstrapExchangeResponse(
        instalacion_id=installation.instalacion_id,
        licencia=raw_license,
        servidor_central=CONTROL_PUBLIC_URL,
    )


@heartbeat_router.post("/heartbeat", response_model=LicenseHeartbeatResponse)
async def heartbeat(
    payload: LicenseHeartbeatRequest,
    x_license_key: str = Header(default="", alias="X-License-Key"),
    db: AsyncSession = Depends(get_db),
):
    _ensure_central()
    installation = (
        await db.execute(
            select(InstalacionSistemaModel).where(
                InstalacionSistemaModel.instalacion_id == payload.instalacion_id
            )
        )
    ).scalar_one_or_none()
    if installation is None or not x_license_key or not compare_digest(
        installation.licencia_hash,
        _hash_license(x_license_key),
    ):
        raise HTTPException(status_code=401, detail="Licencia inválida")

    installation.version_actual = payload.version_actual
    installation.ultima_conexion = _now()
    installation.canal = payload.canal
    if payload.dominio:
        installation.dominio = payload.dominio
    if payload.nombre_isp:
        installation.nombre_isp = payload.nombre_isp
    await db.commit()

    enabled = installation.estado == "activa"
    return LicenseHeartbeatResponse(
        estado=installation.estado,
        mensaje="Licencia activa" if enabled else f"Licencia {installation.estado}",
        version_actual=payload.version_actual,
        version_objetivo=installation.version_objetivo,
        actualizacion_disponible=enabled and update_available(
            payload.version_actual,
            installation.version_objetivo,
        ),
        notas_actualizacion=installation.notas_actualizacion,
    )


@router.get("/instalaciones", response_model=list[InstallationResponse])
async def list_installations(db: AsyncSession = Depends(get_db)):
    _ensure_central()
    return (
        await db.execute(
            select(InstalacionSistemaModel).order_by(
                desc(InstalacionSistemaModel.ultima_conexion),
                desc(InstalacionSistemaModel.id),
            )
        )
    ).scalars().all()


@router.post("/instalaciones", response_model=InstallationCreated, status_code=201)
async def create_installation(
    payload: InstallationCreate,
    db: AsyncSession = Depends(get_db),
):
    _ensure_central()
    raw_license = f"fdz_live_{secrets.token_urlsafe(32)}"
    installation = InstalacionSistemaModel(
        instalacion_id=str(uuid4()),
        nombre_isp=payload.nombre_isp.strip(),
        dominio=payload.dominio.strip() if payload.dominio else None,
        contacto_email=(payload.contacto_email.strip() if payload.contacto_email else None),
        licencia_hash=_hash_license(raw_license),
        plan=payload.plan.strip(),
        estado="activa",
        canal="stable",
    )
    bootstrap_token, bootstrap_expires = _assign_bootstrap(installation)
    db.add(installation)
    await db.commit()
    await db.refresh(installation)
    response = InstallationResponse.model_validate(installation).model_dump()
    return InstallationCreated(
        **response,
        licencia=raw_license,
        token_instalacion=bootstrap_token,
        token_expira=bootstrap_expires,
    )


@router.post(
    "/instalaciones/{installation_id}/bootstrap",
    response_model=BootstrapTokenResponse,
)
async def regenerate_bootstrap(
    installation_id: int,
    db: AsyncSession = Depends(get_db),
):
    _ensure_central()
    installation = await db.get(InstalacionSistemaModel, installation_id)
    if installation is None:
        raise HTTPException(status_code=404, detail="Instalación no encontrada")
    if installation.estado != "activa":
        raise HTTPException(status_code=409, detail="La instalación no está activa")
    token, expires = _assign_bootstrap(installation)
    await db.commit()
    return BootstrapTokenResponse(token_instalacion=token, token_expira=expires)


@router.patch("/instalaciones/{installation_id}", response_model=InstallationResponse)
async def update_installation(
    installation_id: int,
    payload: InstallationUpdate,
    db: AsyncSession = Depends(get_db),
):
    _ensure_central()
    installation = await db.get(InstalacionSistemaModel, installation_id)
    if installation is None:
        raise HTTPException(status_code=404, detail="Instalación no encontrada")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(installation, field, value.strip() if isinstance(value, str) else value)
    await db.commit()
    await db.refresh(installation)
    return installation
