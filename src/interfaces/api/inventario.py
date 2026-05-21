from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List
from pydantic import BaseModel

# 🔥 1. IMPORTAMOS EL CACHÉ
from fastapi_cache.decorator import cache

from src.domain.schemas import ONURetorno
from src.infrastructure.database import get_db
# 👇 Importamos nuestro nuevo servicio
from src.application.services.inventario_service import InventarioService 

router = APIRouter(prefix="/inventario", tags=["Inventario de Equipos"])

# --- ESQUEMAS PYDANTIC ---
class ONUCrear(BaseModel):
    identificador: str
    tecnologia: str 
    modelo: str = "Genérico"


# --- RUTAS ---

# ==========================================
# ESCRITURA (SIN CACHÉ)
# ==========================================
@router.post("/", response_model=ONURetorno)
async def registrar_onu(onu_data: ONUCrear, db: AsyncSession = Depends(get_db)):
    service = InventarioService(db)
    try:
        nueva_onu = await service.registrar_equipo(
            identificador=onu_data.identificador,
            tecnologia=onu_data.tecnologia,
            modelo=onu_data.modelo
        )
        return nueva_onu
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.delete("/{onu_id}")
async def eliminar_onu(onu_id: int, db: AsyncSession = Depends(get_db)):
    service = InventarioService(db)
    try:
        mensaje = await service.eliminar_equipo(onu_id)
        return {"status": "success", "message": mensaje}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ==========================================
# CATÁLOGO DE INVENTARIO (CON CACHÉ)
# ==========================================
@router.get("/", response_model=List[ONURetorno])
@cache(expire=300) # 🔥 Guardamos el inventario por 5 minutos
async def obtener_inventario(estado: str = None, db: AsyncSession = Depends(get_db)):
    service = InventarioService(db)
    return await service.obtener_equipos(estado)