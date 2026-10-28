"""Límites del plan de un ISP en la nube.

El panel central los escribe en el api.env de cada instancia
(FDEZNET_LIMITE_CLIENTES y FDEZNET_LIMITE_ROUTERS). Vacíos = sin límite.
"""
import os
from typing import Optional

from fastapi import Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.database import get_db
from src.infrastructure.models import ClienteModel, RouterModel


def _limite(variable: str) -> Optional[int]:
    valor = os.getenv(variable, "").strip()
    return int(valor) if valor.isdigit() else None


async def remaining_client_capacity(db: AsyncSession) -> Optional[int]:
    limite = _limite("FDEZNET_LIMITE_CLIENTES")
    if limite is None:
        return None
    actuales = await db.scalar(select(func.count(ClienteModel.id))) or 0
    return max(0, limite - actuales)


async def ensure_client_capacity(db: AsyncSession = Depends(get_db)) -> None:
    if await remaining_client_capacity(db) == 0:
        raise HTTPException(
            status_code=403,
            detail=f"Tu plan permite máximo {_limite('FDEZNET_LIMITE_CLIENTES')} abonados",
        )


async def ensure_router_capacity(db: AsyncSession = Depends(get_db)) -> None:
    limite = _limite("FDEZNET_LIMITE_ROUTERS")
    if limite is None:
        return
    actuales = await db.scalar(select(func.count(RouterModel.id))) or 0
    if actuales >= limite:
        raise HTTPException(
            status_code=403,
            detail=f"Tu plan permite máximo {limite} routers",
        )
