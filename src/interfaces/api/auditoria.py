from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.database import get_db
from src.infrastructure.models import LogActividadModel


router = APIRouter(prefix="/auditoria", tags=["Auditoría"])


@router.get("/")
async def listar_actividad(
    usuario_id: Optional[int] = None,
    accion: Optional[str] = Query(default=None, max_length=100),
    desde: Optional[datetime] = None,
    hasta: Optional[datetime] = None,
    pagina: int = Query(default=1, ge=1),
    por_pagina: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    filtros = []
    if usuario_id is not None:
        filtros.append(LogActividadModel.usuario_id == usuario_id)
    if accion:
        filtros.append(func.lower(LogActividadModel.accion).like(f"%{accion.lower()}%"))
    if desde:
        filtros.append(LogActividadModel.fecha >= desde)
    if hasta:
        filtros.append(LogActividadModel.fecha <= hasta)

    total = await db.scalar(
        select(func.count(LogActividadModel.id)).where(*filtros)
    )
    resultado = await db.execute(
        select(LogActividadModel)
        .where(*filtros)
        .order_by(LogActividadModel.fecha.desc(), LogActividadModel.id.desc())
        .offset((pagina - 1) * por_pagina)
        .limit(por_pagina)
    )

    items = resultado.scalars().all()
    return {
        "items": [
            {
                "id": item.id,
                "usuario_id": item.usuario_id,
                "usuario": item.usuario_nombre,
                "accion": item.accion,
                "metodo": item.metodo,
                "ruta": item.ruta,
                "estado_http": item.estado_http,
                "detalle": item.detalle,
                "ip_cliente": item.ip_cliente,
                "fecha": item.fecha,
            }
            for item in items
        ],
        "total": total or 0,
        "pagina": pagina,
        "por_pagina": por_pagina,
    }
