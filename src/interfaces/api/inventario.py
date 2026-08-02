from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List
from pydantic import BaseModel

from src.domain.schemas import ONURetorno
from src.infrastructure.database import get_db
# 👇 Importamos nuestro nuevo servicio
from src.application.services.inventario_service import InventarioService 
from src.infrastructure.auth import role_required

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
async def registrar_onu(
    onu_data: ONUCrear,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    service = InventarioService(db)
    try:
        nueva_onu = await service.registrar_equipo(
            identificador=onu_data.identificador,
            tecnologia=onu_data.tecnologia,
            modelo=onu_data.modelo,
            usuario_id=current_user.id,
        )
        return nueva_onu
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.delete("/{onu_id}")
async def eliminar_onu(
    onu_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin"])),
):
    service = InventarioService(db)
    try:
        mensaje = await service.eliminar_equipo(
            onu_id,
            usuario_id=current_user.id,
        )
        return {"status": "success", "message": mensaje}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ==========================================
# CATÁLOGO DE INVENTARIO
# ==========================================
@router.get("/", response_model=List[ONURetorno])
async def obtener_inventario(
    estado: str = None,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(
        role_required(["admin", "supervisor", "tecnico"])
    ),
):
    if current_user.rol == "tecnico":
        if (estado or "").strip().upper() != "DISPONIBLE":
            raise HTTPException(
                status_code=403,
                detail=(
                    "El técnico solo puede consultar equipos disponibles"
                ),
            )
    service = InventarioService(db)
    return await service.obtener_equipos(estado)
