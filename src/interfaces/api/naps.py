from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

# 🔥 1. IMPORTAMOS EL CACHÉ
from fastapi_cache.decorator import cache

from src.infrastructure.database import get_db
from src.domain.schemas import CajaNapCreate, CajaNapResponse
from src.application.services.nap_service import NapService
from src.infrastructure.auth import role_required

router = APIRouter(prefix="/infraestructura", tags=["Cajas NAP y Fibra"])


class PuertoNapOcupadoResponse(BaseModel):
    id: int
    nombre: str
    puerto_nap: int
    cedula: Optional[str] = None

# ==========================================
# CATÁLOGO DE NAPs (CON CACHÉ)
# ==========================================
@router.get("/naps", response_model=List[CajaNapResponse])
@cache(expire=300) # 🔥 Guardamos el catálogo de cajas por 5 minutos
async def listar_cajas_nap(
    zona_id: int = None, 
    db: AsyncSession = Depends(get_db),
    current_user=Depends(
        role_required(["admin", "supervisor", "tecnico"])
    ),
):
    """Obtiene el inventario de NAPs con ocupación en tiempo real."""
    service = NapService(db)
    return await service.listar_naps(zona_id)


# ==========================================
# ESCRITURA (SIN CACHÉ)
# ==========================================
@router.post("/naps", response_model=CajaNapResponse)
async def crear_caja_nap(
    data: CajaNapCreate, 
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
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
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin"])),
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


# ==========================================
# PUERTOS Y OCUPACIÓN EN VIVO (ESTRICTAMENTE SIN CACHÉ)
# ==========================================
@router.get(
    "/naps/{id}/detalles",
    response_model=List[PuertoNapOcupadoResponse],
)
# 🚫 SIN CACHÉ: Los puertos disponibles se deben consultar en tiempo real para evitar choques
async def obtener_clientes_por_nap(
    id: int, 
    db: AsyncSession = Depends(get_db),
    current_user=Depends(
        role_required(["admin", "supervisor", "tecnico"])
    ),
):
    """Endpoint para el modal visual: devuelve qué cliente está en qué puerto."""
    service = NapService(db)
    return await service.obtener_detalles_nap(id)
