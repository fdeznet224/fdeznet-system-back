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
    LicensePlanCreate,
    LicensePlanResponse,
    LicensePlanUpdate,
    LicensePaymentResponse,
    SubscriptionRenewRequest,
    SystemReleaseCreate,
    SystemReleaseResponse,
    UpdateManifestResponse,
    UpdateReportRequest,
)
from src.infrastructure.database import get_db
from src.infrastructure.models import (
    InstalacionSistemaModel,
    PagoLicenciaModel,
    PlanLicenciaModel,
    VersionSistemaModel,
)


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


def _subscription_state(installation: InstalacionSistemaModel) -> tuple[str, str]:
    if installation.estado != "activa":
        return installation.estado, f"Licencia {installation.estado}"
    if (
        getattr(installation, "plan_tipo", None) == "permanente"
        or getattr(installation, "suscripcion_vence", None) is None
    ):
        return "activa", "Licencia activa"
    now = _now()
    expiry = installation.suscripcion_vence
    if now <= expiry:
        return "activa", f"Plan vigente hasta {expiry:%d/%m/%Y}"
    gracia_hasta = expiry + timedelta(
        days=getattr(installation, "dias_gracia", 0) or 0
    )
    if now <= gracia_hasta:
        return "gracia", f"Mensualidad vencida; periodo de gracia hasta {gracia_hasta:%d/%m/%Y}"
    return "vencida", "Tu mensualidad terminó. Renueva para continuar"


def _apply_plan(
    installation: InstalacionSistemaModel,
    plan: PlanLicenciaModel,
    *,
    months: int = 1,
) -> None:
    now = _now()
    installation.plan_licencia_id = plan.id
    installation.plan = plan.codigo
    installation.plan_nombre = plan.nombre
    installation.plan_tipo = plan.tipo
    installation.precio_mensual = plan.precio_mensual
    installation.limite_clientes = plan.limite_clientes
    installation.limite_routers = plan.limite_routers
    installation.dias_gracia = plan.dias_gracia
    installation.suscripcion_inicio = installation.suscripcion_inicio or now
    if plan.tipo == "permanente":
        installation.suscripcion_vence = None
    else:
        base = max(now, installation.suscripcion_vence or now)
        installation.suscripcion_vence = base + timedelta(
            days=(plan.duracion_dias or 30) * months
        )


def _installation_response(installation: InstalacionSistemaModel) -> dict:
    response = InstallationResponse.model_validate(installation).model_dump()
    response["estado_suscripcion"] = _subscription_state(installation)[0]
    return response


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
        or _subscription_state(installation)[0] == "vencida"
    ):
        raise HTTPException(status_code=401, detail="Token de instalación inválido o vencido")

    release_query = select(VersionSistemaModel).where(
        VersionSistemaModel.activa.is_(True)
    )
    if installation.version_objetivo:
        release_query = release_query.where(
            VersionSistemaModel.version == installation.version_objetivo
        )
    else:
        release_query = release_query.order_by(desc(VersionSistemaModel.id)).limit(1)
    release = (await db.execute(release_query)).scalar_one_or_none()
    if release is None:
        raise HTTPException(
            status_code=503,
            detail="No existe una versión activa para instalar",
        )

    raw_license = f"fdz_live_{secrets.token_urlsafe(32)}"
    installation.licencia_hash = _hash_license(raw_license)
    installation.bootstrap_usado_en = now
    installation.version_objetivo = release.version
    await db.commit()
    return BootstrapExchangeResponse(
        instalacion_id=installation.instalacion_id,
        licencia=raw_license,
        servidor_central=CONTROL_PUBLIC_URL,
        nombre_isp=installation.nombre_isp,
        dominio=installation.dominio,
        contacto_email=installation.contacto_email,
        version=release.version,
        backend_commit=release.backend_commit,
        frontend_commit=release.frontend_commit,
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
    installation.uso_clientes = payload.uso_clientes
    installation.uso_routers = payload.uso_routers
    if payload.dominio:
        installation.dominio = payload.dominio
    if payload.nombre_isp:
        installation.nombre_isp = payload.nombre_isp
    await db.commit()

    effective_state, message = _subscription_state(installation)
    enabled = effective_state in {"activa", "gracia"}
    return LicenseHeartbeatResponse(
        estado=effective_state,
        mensaje=message,
        version_actual=payload.version_actual,
        version_objetivo=installation.version_objetivo,
        actualizacion_disponible=enabled and update_available(
            payload.version_actual,
            installation.version_objetivo,
        ),
        notas_actualizacion=installation.notas_actualizacion,
        plan_nombre=installation.plan_nombre,
        plan_tipo=installation.plan_tipo,
        vigente_hasta=installation.suscripcion_vence,
        dias_gracia=installation.dias_gracia or 0,
        limite_clientes=installation.limite_clientes,
        limite_routers=installation.limite_routers,
        uso_clientes=installation.uso_clientes or 0,
        uso_routers=installation.uso_routers or 0,
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
        _subscription_state(installation)[0] not in {"activa", "gracia"}
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
    installations = (
        await db.execute(
            select(InstalacionSistemaModel).order_by(
                desc(InstalacionSistemaModel.ultima_conexion),
                desc(InstalacionSistemaModel.id),
            )
        )
    ).scalars().all()
    return [_installation_response(item) for item in installations]


@router.get("/planes", response_model=list[LicensePlanResponse])
async def list_license_plans(db: AsyncSession = Depends(get_db)):
    _ensure_central()
    return (
        await db.execute(
            select(PlanLicenciaModel).order_by(
                PlanLicenciaModel.activo.desc(), PlanLicenciaModel.id
            )
        )
    ).scalars().all()


@router.post("/planes", response_model=LicensePlanResponse, status_code=201)
async def create_license_plan(
    payload: LicensePlanCreate,
    db: AsyncSession = Depends(get_db),
):
    _ensure_central()
    plan = PlanLicenciaModel(**payload.model_dump())
    db.add(plan)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="El código del plan ya existe") from exc
    await db.refresh(plan)
    return plan


@router.patch("/planes/{plan_id}", response_model=LicensePlanResponse)
async def update_license_plan(
    plan_id: int,
    payload: LicensePlanUpdate,
    db: AsyncSession = Depends(get_db),
):
    _ensure_central()
    plan = await db.get(PlanLicenciaModel, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan no encontrado")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(plan, field, value)
    await db.commit()
    await db.refresh(plan)
    return plan


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
    plan = None
    if payload.plan_licencia_id:
        plan = await db.get(PlanLicenciaModel, payload.plan_licencia_id)
    if plan is None:
        plan = await db.scalar(
            select(PlanLicenciaModel).where(
                PlanLicenciaModel.codigo == payload.plan,
                PlanLicenciaModel.activo.is_(True),
            )
        )
    if plan is None:
        plan = await db.scalar(
            select(PlanLicenciaModel).where(
                PlanLicenciaModel.codigo == "demo",
                PlanLicenciaModel.activo.is_(True),
            )
        )
    if plan is None or not plan.activo:
        raise HTTPException(status_code=400, detail="Selecciona un plan activo")
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
    _apply_plan(installation, plan)
    bootstrap_token, bootstrap_expires = _assign_bootstrap(installation)
    db.add(installation)
    await db.commit()
    await db.refresh(installation)
    response = _installation_response(installation)
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
    update_data = payload.model_dump(exclude_unset=True)
    plan_id = update_data.pop("plan_licencia_id", None)
    if plan_id is not None:
        plan = await db.get(PlanLicenciaModel, plan_id)
        if plan is None or not plan.activo:
            raise HTTPException(status_code=400, detail="Plan no disponible")
        _apply_plan(installation, plan, months=1)
    for field, value in update_data.items():
        setattr(installation, field, value.strip() if isinstance(value, str) else value)
    await db.commit()
    await db.refresh(installation)
    return _installation_response(installation)


@router.post(
    "/instalaciones/{installation_id}/renovar",
    response_model=InstallationResponse,
)
async def renew_subscription(
    installation_id: int,
    payload: SubscriptionRenewRequest,
    db: AsyncSession = Depends(get_db),
):
    _ensure_central()
    installation = await db.get(InstalacionSistemaModel, installation_id)
    if installation is None:
        raise HTTPException(status_code=404, detail="Instalación no encontrada")
    plan_id = payload.plan_licencia_id or installation.plan_licencia_id
    plan = await db.get(PlanLicenciaModel, plan_id) if plan_id else None
    if plan is None or not plan.activo:
        raise HTTPException(status_code=400, detail="Selecciona un plan activo")
    _apply_plan(installation, plan, months=payload.meses)
    db.add(
        PagoLicenciaModel(
            instalacion_id=installation.id,
            plan_licencia_id=plan.id,
            meses=payload.meses,
            monto=(
                payload.monto
                if payload.monto is not None
                else plan.precio_mensual * payload.meses
            ),
            referencia=payload.referencia.strip() if payload.referencia else None,
        )
    )
    await db.commit()
    await db.refresh(installation)
    return _installation_response(installation)


@router.get(
    "/instalaciones/{installation_id}/pagos",
    response_model=list[LicensePaymentResponse],
)
async def list_license_payments(
    installation_id: int,
    db: AsyncSession = Depends(get_db),
):
    _ensure_central()
    return (
        await db.execute(
            select(PagoLicenciaModel)
            .where(PagoLicenciaModel.instalacion_id == installation_id)
            .order_by(desc(PagoLicenciaModel.id))
        )
    ).scalars().all()
