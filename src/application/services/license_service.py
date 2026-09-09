import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.schemas import LicenseHeartbeatRequest, LocalLicenseStatus
from src.application.services.branding_service import get_or_create_system_config
from src.infrastructure.models import ClienteModel, ConfiguracionSistema, RouterModel
from src.infrastructure.database import get_db
from src.version import SYSTEM_VERSION, UPDATE_CHANNEL


CONTROL_URL = os.getenv("FDEZNET_CONTROL_URL", "https://fdezpay.com/api").rstrip("/")
INSTALLATION_ID = os.getenv("FDEZNET_INSTALLATION_ID", "").strip()
LICENSE_KEY = os.getenv("FDEZNET_LICENSE_KEY", "").strip()
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip() or None
CONTROL_PLANE_MODE = os.getenv("FDEZNET_CONTROL_PLANE_MODE", "client").strip().lower()


def _version_tuple(version: Optional[str]) -> tuple[int, ...]:
    if not version:
        return ()
    clean = version.strip().lower().lstrip("v")
    try:
        return tuple(int(part) for part in clean.split("."))
    except ValueError:
        return ()


def update_available(current: str, target: Optional[str]) -> bool:
    current_tuple = _version_tuple(current)
    target_tuple = _version_tuple(target)
    return bool(current_tuple and target_tuple and target_tuple > current_tuple)


async def _config(db: AsyncSession) -> ConfiguracionSistema:
    return await get_or_create_system_config(db)


def effective_local_state(config: ConfiguracionSistema) -> tuple[str, str]:
    state = config.licencia_estado
    message = config.licencia_mensaje or "Licencia activa"
    if state in {"suspendida", "revocada", "vencida"}:
        return state, message
    expiry = getattr(config, "licencia_vigente_hasta", None)
    if getattr(config, "licencia_tipo", None) == "permanente" or expiry is None:
        return state, message
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if now <= expiry:
        return "activa", f"Plan vigente hasta {expiry:%d/%m/%Y}"
    grace_until = expiry + timedelta(
        days=getattr(config, "licencia_dias_gracia", 0) or 0
    )
    if now <= grace_until:
        return "gracia", f"Mensualidad vencida; periodo de gracia hasta {grace_until:%d/%m/%Y}"
    return "vencida", "Tu mensualidad terminó. Renueva para continuar"


async def require_valid_license(
    db: AsyncSession = Depends(get_db),
) -> None:
    if CONTROL_PLANE_MODE == "central":
        return
    if not INSTALLATION_ID or not LICENSE_KEY:
        raise HTTPException(status_code=503, detail="Licencia no configurada")
    config = await _config(db)
    state, message = effective_local_state(config)
    if state in {"suspendida", "revocada", "vencida"}:
        raise HTTPException(
            status_code=403,
            detail=message,
        )


def local_status(config: ConfiguracionSistema) -> LocalLicenseStatus:
    is_central = CONTROL_PLANE_MODE == "central"
    configured = is_central or bool(INSTALLATION_ID and LICENSE_KEY)
    target = config.version_disponible
    effective_state, effective_message = effective_local_state(config)
    return LocalLicenseStatus(
        configurada=configured,
        instalacion_id=INSTALLATION_ID or ("control-central" if is_central else None),
        servidor_central=CONTROL_URL,
        estado="servidor_central" if is_central else (effective_state if configured else "sin_configurar"),
        mensaje="Servidor central de licencias" if is_central else (effective_message or (
            "Licencia lista" if configured else "Faltan las credenciales de esta instalación"
        )),
        version_actual=SYSTEM_VERSION,
        version_objetivo=target,
        actualizacion_disponible=update_available(SYSTEM_VERSION, target),
        notas_actualizacion=config.notas_actualizacion,
        ultima_revision=config.licencia_ultima_revision,
        plan_nombre=config.licencia_plan,
        plan_tipo=config.licencia_tipo,
        vigente_hasta=config.licencia_vigente_hasta,
        dias_gracia=config.licencia_dias_gracia or 0,
        limite_clientes=config.licencia_limite_clientes,
        limite_routers=config.licencia_limite_routers,
        uso_clientes=config.licencia_uso_clientes or 0,
        uso_routers=config.licencia_uso_routers or 0,
    )


async def remaining_client_capacity(db: AsyncSession) -> Optional[int]:
    if CONTROL_PLANE_MODE == "central":
        return None
    config = await _config(db)
    limit = config.licencia_limite_clientes
    if limit is None:
        return None
    current = await db.scalar(select(func.count(ClienteModel.id))) or 0
    return max(0, limit - current)


async def ensure_client_capacity(db: AsyncSession = Depends(get_db)) -> None:
    remaining = await remaining_client_capacity(db)
    if remaining == 0:
        config = await _config(db)
        raise HTTPException(
            status_code=403,
            detail=f"Tu plan permite máximo {config.licencia_limite_clientes} abonados",
        )


async def ensure_router_capacity(db: AsyncSession = Depends(get_db)) -> None:
    if CONTROL_PLANE_MODE == "central":
        return
    config = await _config(db)
    limit = config.licencia_limite_routers
    if limit is None:
        return
    current = await db.scalar(select(func.count(RouterModel.id))) or 0
    if current >= limit:
        raise HTTPException(
            status_code=403,
            detail=f"Tu plan permite máximo {limit} routers",
        )


async def verify_license(db: AsyncSession) -> LocalLicenseStatus:
    config = await _config(db)
    if CONTROL_PLANE_MODE == "central":
        return local_status(config)
    if not INSTALLATION_ID or not LICENSE_KEY:
        config.licencia_estado = "sin_configurar"
        config.licencia_mensaje = "Configura FDEZNET_INSTALLATION_ID y FDEZNET_LICENSE_KEY"
        await db.commit()
        return local_status(config)

    client_count = await db.scalar(select(func.count(ClienteModel.id))) or 0
    router_count = await db.scalar(select(func.count(RouterModel.id))) or 0
    payload = LicenseHeartbeatRequest(
        instalacion_id=INSTALLATION_ID,
        version_actual=SYSTEM_VERSION,
        dominio=PUBLIC_URL,
        nombre_isp=config.empresa_nombre,
        canal=UPDATE_CHANNEL,
        uso_clientes=client_count,
        uso_routers=router_count,
    )
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"{CONTROL_URL}/control/heartbeat",
                json=payload.model_dump(),
                headers={"X-License-Key": LICENSE_KEY},
            )
        response.raise_for_status()
        result = response.json()
        config.licencia_estado = result["estado"]
        config.licencia_mensaje = result["mensaje"]
        config.version_disponible = result.get("version_objetivo")
        config.notas_actualizacion = result.get("notas_actualizacion")
        config.licencia_plan = result.get("plan_nombre")
        config.licencia_tipo = result.get("plan_tipo")
        expiry = result.get("vigente_hasta")
        config.licencia_vigente_hasta = (
            datetime.fromisoformat(expiry.replace("Z", "+00:00")).replace(tzinfo=None)
            if isinstance(expiry, str) and expiry
            else expiry
        )
        config.licencia_dias_gracia = result.get("dias_gracia", 0)
        config.licencia_limite_clientes = result.get("limite_clientes")
        config.licencia_limite_routers = result.get("limite_routers")
        config.licencia_uso_clientes = client_count
        config.licencia_uso_routers = router_count
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        config.licencia_mensaje = f"No se pudo consultar el servidor central: {type(exc).__name__}"
        if config.licencia_estado == "sin_configurar":
            config.licencia_estado = "sin_conexion"
    config.licencia_ultima_revision = datetime.now(timezone.utc).replace(tzinfo=None)
    await db.commit()
    await db.refresh(config)
    return local_status(config)
