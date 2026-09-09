import hashlib
import hmac
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
from sqlalchemy.exc import IntegrityError

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
    SystemReleaseCreate,
    SystemReleaseResponse,
    UpdateManifestResponse,
    UpdateReportRequest,
)
from src.infrastructure.database import get_db
from src.infrastructure.models import InstalacionSistemaModel, VersionSistemaModel


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


def _sign_update_manifest(
    license_hash: str,
    version: str,
    backend_commit: str,
    frontend_commit: str,
) -> str:
    canonical = f"{version}|{backend_commit}|{frontend_commit}"
    return hmac.new(
        bytes.fromhex(license_hash),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _authenticate_installation(
    db: AsyncSession,
    installation_id: str,
    license_key: str,
) -> InstalacionSistemaModel:
    installation = (
        await db.execute(
            select(InstalacionSistemaModel).where(
                InstalacionSistemaModel.instalacion_id == installation_id
            )
        )
    ).scalar_one_or_none()
    if installation is None or not license_key or not compare_digest(
        installation.licencia_hash,
        _hash_license(license_key),
    ):
        raise HTTPException(status_code=401, detail="Licencia inválida")
    return installation


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
        nombre_isp=installation.nombre_isp,
        dominio=installation.dominio,
        contacto_email=installation.contacto_email,
    )


@heartbeat_router.post("/heartbeat", response_model=LicenseHeartbeatResponse)
async def heartbeat(
    payload: LicenseHeartbeatRequest,
    x_license_key: str = Header(default="", alias="X-License-Key"),
    db: AsyncSession = Depends(get_db),
):
    _ensure_central()
    installation = await _authenticate_installation(
        db,
        payload.instalacion_id,
        x_license_key,
    )

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


@heartbeat_router.get("/update-manifest", response_model=UpdateManifestResponse)
async def update_manifest(
    x_installation_id: str = Header(default="", alias="X-Installation-ID"),
    x_license_key: str = Header(default="", alias="X-License-Key"),
    x_update_requested: str = Header(default="", alias="X-Update-Requested"),
    db: AsyncSession = Depends(get_db),
):
    _ensure_central()
    installation = await _authenticate_installation(
        db,
        x_installation_id,
        x_license_key,
    )
    if (
        installation.estado != "activa"
        or not (
            installation.actualizacion_automatica
            or compare_digest(x_update_requested.strip().lower(), "true")
        )
        or not installation.version_objetivo
        or not update_available(
            installation.version_actual or "0.0.0",
            installation.version_objetivo,
        )
    ):
        return UpdateManifestResponse(actualizacion_disponible=False)
    release = (
        await db.execute(
            select(VersionSistemaModel).where(
                VersionSistemaModel.version == installation.version_objetivo,
                VersionSistemaModel.activa.is_(True),
            )
        )
    ).scalar_one_or_none()
    if release is None:
        return UpdateManifestResponse(actualizacion_disponible=False)
    signature = _sign_update_manifest(
        installation.licencia_hash,
        release.version,
        release.backend_commit,
        release.frontend_commit,
    )
    return UpdateManifestResponse(
        actualizacion_disponible=True,
        version=release.version,
        backend_commit=release.backend_commit,
        frontend_commit=release.frontend_commit,
        notas=release.notas,
        firma=signature,
    )


@heartbeat_router.post("/update-report", status_code=204)
async def update_report(
    payload: UpdateReportRequest,
    x_installation_id: str = Header(default="", alias="X-Installation-ID"),
    x_license_key: str = Header(default="", alias="X-License-Key"),
    db: AsyncSession = Depends(get_db),
):
    _ensure_central()
    installation = await _authenticate_installation(
        db,
        x_installation_id,
        x_license_key,
    )
    installation.actualizacion_estado = payload.estado
    installation.actualizacion_mensaje = payload.mensaje
    installation.actualizacion_fecha = _now()
    if payload.respaldo:
        installation.ultimo_respaldo = payload.respaldo
    if payload.estado == "exitosa" and payload.version:
        installation.version_actual = payload.version
    await db.commit()


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


@router.get("/versiones", response_model=list[SystemReleaseResponse])
async def list_releases(db: AsyncSession = Depends(get_db)):
    _ensure_central()
    return (
        await db.execute(
            select(VersionSistemaModel).order_by(desc(VersionSistemaModel.id))
        )
    ).scalars().all()


@router.post("/versiones", response_model=SystemReleaseResponse, status_code=201)
async def create_release(
    payload: SystemReleaseCreate,
    db: AsyncSession = Depends(get_db),
):
    _ensure_central()
    release = VersionSistemaModel(**payload.model_dump(), activa=True)
    db.add(release)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="La versión ya existe") from exc
    await db.refresh(release)
    return release


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
    if payload.version_objetivo:
        release_exists = await db.scalar(
            select(VersionSistemaModel.id).where(
                VersionSistemaModel.version == payload.version_objetivo,
                VersionSistemaModel.activa.is_(True),
            )
        )
        if release_exists is None:
            raise HTTPException(status_code=400, detail="La versión no está publicada")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(installation, field, value.strip() if isinstance(value, str) else value)
    await db.commit()
    await db.refresh(installation)
    return installation
