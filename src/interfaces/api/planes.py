from typing import List
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

# 🔥 1. IMPORTAMOS EL DECORADOR DE CACHÉ
from fastapi_cache.decorator import cache

# Infraestructura y Auth
from src.infrastructure.database import get_db
from src.infrastructure.auth import role_required 

# Schemas
from src.domain.schemas import PlanCreate, PlanResponse

# Lógica de Negocio
from src.application.services.plan_service import PlanService
from src.infrastructure.repositories import PlanRepository

router = APIRouter(prefix="/planes", tags=["Catálogo - Planes de Internet"])

# ==========================================
# 1. LECTURA (¡AQUÍ VA EL CACHÉ!)
# ==========================================

@router.get("/", response_model=List[PlanResponse])
@cache(expire=300) # 🔥 2. Guardamos la lista general por 5 minutos
async def listar_planes(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(
        role_required(["admin", "supervisor", "tecnico"])
    ),
):
    repo = PlanRepository(db)
    return await repo.get_all_planes()

@router.get("/router/{router_id}", response_model=List[PlanResponse])
async def listar_planes_por_router(
    router_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(
        role_required(["admin", "supervisor", "tecnico"])
    ),
):
    repo = PlanRepository(db)
    return await repo.get_planes_by_router(router_id)

# ==========================================
# 2. ESCRITURA (ADMIN) - SIN CACHÉ
# ==========================================

@router.post("/", response_model=PlanResponse)
async def crear_plan(
    plan: PlanCreate, 
    db: AsyncSession = Depends(get_db),
    current_user = Depends(role_required(["admin"]))
):
    service = PlanService(db)
    try:
        return await service.crear_plan(plan)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error interno: {str(e)}")

@router.put("/{plan_id}", response_model=PlanResponse)
async def editar_plan(
    plan_id: int, 
    plan_update: PlanCreate, 
    db: AsyncSession = Depends(get_db),
    current_user = Depends(role_required(["admin"]))
):
    service = PlanService(db)
    try:
        return await service.editar_plan(plan_id, plan_update)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        # Importante: Si falla la conexión a MikroTik, avisa pero guarda en BD
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/{plan_id}")
async def eliminar_plan(
    plan_id: int, 
    db: AsyncSession = Depends(get_db),
    current_user = Depends(role_required(["admin"]))
):
    service = PlanService(db)
    try:
        mensaje = await service.eliminar_plan(plan_id)
        return {"status": "success", "message": mensaje}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
