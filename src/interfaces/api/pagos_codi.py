"""Pagos CoDi con pey.mx.

- webhook_router: aviso de pago de pey. Público: lo protege la firma del webhook.
- router: el personal cobra con QR en campo y ve si ya se pagó.
"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.services.codi_service import (
    VIGENCIA_QR_TECNICO,
    FirmaInvalida,
    codi_disponible,
    crear_qr,
    estado_cobro,
    procesar_webhook,
)
from src.infrastructure.auth import role_required
from src.infrastructure.database import get_db
from src.infrastructure.models import ClienteModel

logger = logging.getLogger(__name__)

webhook_router = APIRouter(prefix="/pagos/codi", tags=["Pagos CoDi"])
router = APIRouter(prefix="/pagos/codi", tags=["Pagos CoDi"])
cobran = role_required(["admin", "supervisor", "cajero", "tecnico"])


@webhook_router.post("/webhook")
async def recibir_pago_codi(request: Request, db: AsyncSession = Depends(get_db)):
    cuerpo = await request.body()
    try:
        return await procesar_webhook(db, request.headers, cuerpo)
    except FirmaInvalida as exc:
        logger.warning("Aviso CoDi rechazado: %s", exc)
        raise HTTPException(status_code=401, detail="Firma inválida")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Cuerpo inválido")
    # Cualquier otro error responde 500 para que pey lo reintente; la clave
    # de idempotencia evita aplicarlo dos veces.


async def _cliente(db: AsyncSession, cliente_id: int) -> ClienteModel:
    cliente = await db.get(ClienteModel, cliente_id)
    if cliente is None:
        raise HTTPException(status_code=404, detail="Cliente no encontrado")
    return cliente


@router.get("/clientes/{cliente_id}")
async def codi_del_cliente(cliente_id: int, db: AsyncSession = Depends(get_db), _usuario=Depends(cobran)):
    """Si a este cliente se le puede cobrar por CoDi (activo y, en prueba, si está en la lista)."""
    return {"disponible": await codi_disponible(db, await _cliente(db, cliente_id))}


@router.post("/clientes/{cliente_id}/qr")
async def qr_en_campo(cliente_id: int, db: AsyncSession = Depends(get_db), _usuario=Depends(cobran)):
    """QR por todo lo que debe, para que el cliente lo escanee desde la pantalla del técnico."""
    datos = await crear_qr(db, await _cliente(db, cliente_id), vigencia=VIGENCIA_QR_TECNICO, origen="tecnico")
    if datos.get("error"):
        raise HTTPException(status_code=409, detail=datos["error"])
    return datos


@router.get("/cobros/{cobro_id}")
async def como_va_el_cobro(cobro_id: int, db: AsyncSession = Depends(get_db), _usuario=Depends(cobran)):
    estado = await estado_cobro(db, cobro_id)
    if estado is None:
        raise HTTPException(status_code=404, detail="Cobro no encontrado")
    return estado
