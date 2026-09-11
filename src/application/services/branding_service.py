import os

from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.models import ConfiguracionSistema


def initial_branding_values() -> dict[str, str | None]:
    company_name = os.getenv("FDEZNET_BRAND_NAME", "").strip() or "Mi ISP"
    system_name = os.getenv("FDEZNET_BRAND_SYSTEM_NAME", "").strip() or company_name
    company_email = os.getenv("FDEZNET_BRAND_EMAIL", "").strip()
    return {
        "empresa_nombre": company_name,
        "sistema_nombre": system_name,
        "empresa_email": company_email or None,
    }


async def get_or_create_system_config(
    db: AsyncSession,
) -> ConfiguracionSistema:
    config = await db.get(ConfiguracionSistema, 1)
    if config is not None:
        return config

    config = ConfiguracionSistema(id=1, **initial_branding_values())
    db.add(config)
    await db.commit()
    await db.refresh(config)
    return config
