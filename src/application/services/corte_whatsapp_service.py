"""Modo de suspensión "solo WhatsApp": el moroso conserva WhatsApp lento.

La preferencia vive en ConfiguracionSistema y se aplica a cada MikroTik con
MikroTikService.inicializar_firewall_corte.
"""

import asyncio
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.mikrotik_service import MikroTikService
from src.infrastructure.models import (
    ConfiguracionSistema,
    LogCronjobModel,
    RouterModel,
)
from src.utils.mikrotik import KBPS_CORTE_WHATSAPP_DEFAULT

ORIGEN_LOG = "CorteWhatsApp"


@dataclass(frozen=True)
class ModoCorte:
    solo_whatsapp: bool = False
    kbps: int = KBPS_CORTE_WHATSAPP_DEFAULT


async def obtener_modo_corte(db: AsyncSession) -> ModoCorte:
    """Lee el modo de corte; ante cualquier duda usa el corte total."""
    try:
        config = await db.get(ConfiguracionSistema, 1)
    except Exception:
        return ModoCorte()
    solo_whatsapp = getattr(config, "corte_solo_whatsapp", False) is True
    kbps = getattr(config, "corte_whatsapp_kbps", None)
    if not isinstance(kbps, int) or kbps <= 0:
        kbps = KBPS_CORTE_WHATSAPP_DEFAULT
    return ModoCorte(solo_whatsapp=solo_whatsapp, kbps=kbps)


def aplicar_modo_corte(mk, modo: ModoCorte) -> tuple[bool, str]:
    return mk.inicializar_firewall_corte(
        solo_whatsapp=modo.solo_whatsapp,
        kbps=modo.kbps,
    )


async def aplicar_modo_corte_en_routers(
    db: AsyncSession,
    mikrotik_factory=MikroTikService,
) -> dict[str, int]:
    """Aplica el modo vigente en todos los routers activos."""
    modo = await obtener_modo_corte(db)
    routers = (
        await db.execute(
            select(RouterModel).where(RouterModel.is_active.is_(True))
        )
    ).scalars().all()
    reporte = {"routers": len(routers), "aplicados": 0, "errores": 0}
    for router in routers:
        mk = mikrotik_factory(
            router.ip_vpn,
            router.user_api,
            router.pass_api,
            router.port_api,
        )
        ok, mensaje = await asyncio.to_thread(aplicar_modo_corte, mk, modo)
        if ok:
            reporte["aplicados"] += 1
        else:
            reporte["errores"] += 1
        db.add(
            LogCronjobModel(
                nivel="INFO" if ok else "ERROR",
                origen=ORIGEN_LOG,
                mensaje=(
                    f"Router '{router.nombre}': {mensaje}"
                    if ok
                    else (
                        f"No se aplicó el modo de corte en "
                        f"'{router.nombre}'; se reintentará en la "
                        f"conciliación: {mensaje}"
                    )
                ),
            )
        )
    await db.commit()
    return reporte


async def tarea_aplicar_modo_corte():
    """Aplica el modo en los routers y ajusta PPPoE/DHCP de los suspendidos."""
    from src.application.services.mikrotik_reconciliation_service import (
        MikrotikReconciliationService,
    )
    from src.infrastructure.database import SessionLocal

    async with SessionLocal() as db:
        await aplicar_modo_corte_en_routers(db)
        await MikrotikReconciliationService(db).ejecutar()
