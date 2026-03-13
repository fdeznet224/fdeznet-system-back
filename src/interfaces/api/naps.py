from typing import List
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.database import get_db
from src.domain.schemas import CajaNapCreate, CajaNapResponse, ClienteResponse
from src.application.services.nap_service import NapService

router = APIRouter(prefix="/infraestructura", tags=["Cajas NAP y Fibra"])

@router.get("/naps", response_model=List[CajaNapResponse])
async def listar_cajas_nap(
    zona_id: int = None, 
    db: AsyncSession = Depends(get_db)
):
    """Obtiene el inventario de NAPs con ocupación en tiempo real."""
    service = NapService(db)
    return await service.listar_naps(zona_id)

@router.post("/naps", response_model=CajaNapResponse)
async def crear_caja_nap(
    data: CajaNapCreate, 
    db: AsyncSession = Depends(get_db)
):
    """Registra una nueva caja de fibra."""
    service = NapService(db)
    try:
        return await service.crear_nap(data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/naps/{id}")
async def eliminar_caja_nap(
    id: int, 
    db: AsyncSession = Depends(get_db)
):
    """Elimina una caja NAP (Solo si no tiene clientes)."""
    service = NapService(db)
    try:
        mensaje = await service.eliminar_nap(id)
        return {"status": "success", "message": mensaje}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/naps/{id}/detalles", response_model=List[ClienteResponse])
async def obtener_clientes_por_nap(
    id: int, 
    db: AsyncSession = Depends(get_db)
):
    """Endpoint para el modal visual: devuelve qué cliente está en qué puerto."""
    service = NapService(db)
    return await service.obtener_detalles_nap(id)