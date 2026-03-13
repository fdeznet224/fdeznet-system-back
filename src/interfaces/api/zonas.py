from typing import List
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

# Infraestructura y Auth
from src.infrastructure.database import get_db
from src.infrastructure.auth import role_required

# Schemas (Unificados)
from src.domain.schemas import ZonaCreate, ZonaResponse

# Servicio
from src.application.services.zone_service import ZoneService

router = APIRouter(prefix="/zonas", tags=["Catálogo - Zonas"])

# ==========================================
# 1. LECTURA (Visible para todos)
# ==========================================

@router.get("/", response_model=List[ZonaResponse])
async def listar_zonas(db: AsyncSession = Depends(get_db)):
    """
    Lista todas las zonas (colonias/sectores) disponibles.
    """
    service = ZoneService(db)
    return await service.listar_zonas()

# ==========================================
# 2. ESCRITURA (Solo Admin)
# ==========================================

@router.post("/", response_model=ZonaResponse)
async def crear_zona(
    datos: ZonaCreate, 
    db: AsyncSession = Depends(get_db),
    current_user = Depends(role_required(["admin"])) # 🔒 Solo Admin
):
    """
    Registra una nueva zona.
    """
    service = ZoneService(db)
    try:
        return await service.crear_zona(datos)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.put("/{zona_id}", response_model=ZonaResponse)
async def editar_zona(
    zona_id: int, 
    datos: ZonaCreate, 
    db: AsyncSession = Depends(get_db),
    current_user = Depends(role_required(["admin"])) # 🔒 Solo Admin
):
    """
    Actualiza el nombre de una zona.
    """
    service = ZoneService(db)
    try:
        # Nota: Asegúrate de tener el método editar_zona en tu servicio
        return await service.editar_zona(zona_id, datos)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/{zona_id}")
async def eliminar_zona(
    zona_id: int, 
    db: AsyncSession = Depends(get_db),
    current_user = Depends(role_required(["admin"])) # 🔒 Solo Admin
):
    """
    Elimina una zona. 
    Fallará si hay clientes asignados a ella.
    """
    service = ZoneService(db)
    try:
        # Nota: Asegúrate de tener el método eliminar_zona en tu servicio
        return await service.eliminar_zona(zona_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))