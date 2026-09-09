import os
from datetime import datetime, timezone
from typing import Optional

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.schemas import LicenseHeartbeatRequest, LocalLicenseStatus
from src.infrastructure.models import ConfiguracionSistema
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
    config = await db.get(ConfiguracionSistema, 1)
    if config is None:
        config = ConfiguracionSistema(id=1)
        db.add(config)
        await db.flush()
    return config


def local_status(config: ConfiguracionSistema) -> LocalLicenseStatus:
    is_central = CONTROL_PLANE_MODE == "central"
    configured = is_central or bool(INSTALLATION_ID and LICENSE_KEY)
    target = config.version_disponible
    return LocalLicenseStatus(
        configurada=configured,
        instalacion_id=INSTALLATION_ID or ("control-central" if is_central else None),
        servidor_central=CONTROL_URL,
        estado="servidor_central" if is_central else (config.licencia_estado if configured else "sin_configurar"),
        mensaje="Servidor central de licencias" if is_central else (config.licencia_mensaje or (
            "Licencia lista" if configured else "Faltan las credenciales de esta instalación"
        )),
        version_actual=SYSTEM_VERSION,
        version_objetivo=target,
        actualizacion_disponible=update_available(SYSTEM_VERSION, target),
        notas_actualizacion=config.notas_actualizacion,
        ultima_revision=config.licencia_ultima_revision,
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

    payload = LicenseHeartbeatRequest(
        instalacion_id=INSTALLATION_ID,
        version_actual=SYSTEM_VERSION,
        dominio=PUBLIC_URL,
        nombre_isp=config.empresa_nombre,
        canal=UPDATE_CHANNEL,
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
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        config.licencia_mensaje = f"No se pudo consultar el servidor central: {type(exc).__name__}"
        if config.licencia_estado == "sin_configurar":
            config.licencia_estado = "sin_conexion"
    config.licencia_ultima_revision = datetime.now(timezone.utc).replace(tzinfo=None)
    await db.commit()
    await db.refresh(config)
    return local_status(config)
