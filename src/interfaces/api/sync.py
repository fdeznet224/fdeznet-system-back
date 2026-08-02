from datetime import datetime
import logging
from typing import Any, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.services.sync_service import (
    SyncConflictError,
    SyncService,
)
from src.infrastructure.auth import get_current_active_user
from src.infrastructure.database import get_db


router = APIRouter(prefix="/sincronizacion", tags=["Sincronización móvil"])
logger = logging.getLogger(__name__)


class OperacionEntrada(BaseModel):
    id: UUID
    tipo: Literal[
        "orden_estado",
        "soporte_incidencia",
        "pago_factura",
    ]
    creado_cliente: Optional[datetime] = None
    payload: dict[str, Any]


class LoteSincronizacion(BaseModel):
    operaciones: list[OperacionEntrada] = Field(min_length=1, max_length=50)


@router.post("/procesar")
async def procesar_lote(
    datos: LoteSincronizacion,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    resultados = []
    ids = [str(item.id) for item in datos.operaciones]
    if len(ids) != len(set(ids)):
        return {
            "resultados": [
                {
                    "id": item.id,
                    "estado": "rechazada",
                    "error": "El lote contiene identificadores duplicados",
                }
                for item in datos.operaciones
            ]
        }

    service = SyncService(db)
    for item in datos.operaciones:
        try:
            resultados.append(
                await service.procesar(
                    operacion_id=str(item.id),
                    tipo=item.tipo,
                    payload=item.payload,
                    creado_cliente=item.creado_cliente,
                    usuario=current_user,
                )
            )
        except SyncConflictError as exc:
            await db.rollback()
            resultados.append(
                {
                    "id": item.id,
                    "estado": "conflicto",
                    "error": str(exc),
                }
            )
        except RuntimeError as exc:
            await db.rollback()
            resultados.append(
                {
                    "id": item.id,
                    "estado": "conflicto",
                    "error": str(exc),
                }
            )
        except (ValidationError, ValueError, PermissionError) as exc:
            await db.rollback()
            resultados.append(
                {
                    "id": item.id,
                    "estado": "rechazada",
                    "error": _mensaje_error(exc),
                }
            )
        except Exception:
            await db.rollback()
            logger.exception(
                "Error procesando operación sincronizada %s",
                item.id,
            )
            resultados.append(
                {
                    "id": item.id,
                    "estado": "error",
                    "error": "No se pudo procesar la operación",
                }
            )
    return {"resultados": resultados}


def _mensaje_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        errores = exc.errors()
        if errores:
            return str(errores[0].get("msg", "Datos inválidos"))
        return "Datos inválidos"
    return str(exc)
