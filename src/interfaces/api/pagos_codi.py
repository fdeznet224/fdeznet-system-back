"""Aviso de pago de pey.mx (CoDi). Público: lo protege la firma del webhook."""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.services.codi_service import FirmaInvalida, procesar_webhook
from src.infrastructure.database import get_db

logger = logging.getLogger(__name__)

webhook_router = APIRouter(prefix="/pagos/codi", tags=["Pagos CoDi"])


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
