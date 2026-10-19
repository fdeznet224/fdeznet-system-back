from datetime import date
from decimal import Decimal
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# Infraestructura y Auth
from src.infrastructure.database import get_db
from src.infrastructure.auth import role_required

# Schemas (Unificados)
from src.domain.schemas import ZonaCreate, ZonaResponse

# Servicio
from src.application.services.zone_service import ZoneService
from src.application.services.resumen_cobro import simular_cobro
from src.infrastructure.models import PlanModel, PlantillaFacturacionModel, ZonaModel

router = APIRouter(prefix="/zonas", tags=["Catálogo - Zonas"])

# ==========================================
# 1. LECTURA (Visible para todos)
# ==========================================

@router.get("/", response_model=List[ZonaResponse])
async def listar_zonas(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    """
    Lista todas las zonas (colonias/sectores) disponibles.
    """
    service = ZoneService(db)
    return await service.listar_zonas()


@router.get("/simulador-cobro")
async def catalogo_simulador_cobro(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor", "tecnico"])),
):
    """Zonas con su día de pago y sus planes, para que el técnico simule el cobro."""
    zonas = (await db.execute(select(ZonaModel).order_by(ZonaModel.nombre))).scalars().all()
    planes = (await db.execute(select(PlanModel).order_by(PlanModel.precio, PlanModel.id))).scalars().all()
    plantillas = {
        p.id: p for p in (await db.execute(select(PlantillaFacturacionModel))).scalars().all()
    }
    salida = []
    for zona in zonas:
        plantilla = plantillas.get(zona.plantilla_id)
        salida.append({
            "id": zona.id,
            "nombre": zona.nombre,
            "plantilla": (
                {"nombre": plantilla.nombre, "dia_pago": plantilla.dia_pago, "dias_tolerancia": plantilla.dias_tolerancia or 0}
                if plantilla else None
            ),
            # Los planes del MikroTik de la zona, igual que al activar.
            "planes": [
                {"id": p.id, "nombre": p.nombre, "precio": float(p.precio or 0), "velocidad_bajada": p.velocidad_bajada}
                for p in planes if zona.router_id and p.router_id == zona.router_id
            ],
        })
    return salida


@router.get("/{zona_id}/simular-cobro")
async def simular_cobro_zona(
    zona_id: int,
    plan_id: int = Query(gt=0),
    fecha: Optional[date] = None,
    meses_gratis: int = Query(default=1, ge=0, le=12),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor", "tecnico"])),
):
    """Cuánto y cuándo pagaría un cliente si se instala ese día. No crea nada."""
    zona = await db.get(ZonaModel, zona_id)
    plan = await db.get(PlanModel, plan_id)
    if not zona or not plan:
        raise HTTPException(status_code=404, detail="Zona o plan no encontrado")
    plantilla = await db.get(PlantillaFacturacionModel, zona.plantilla_id) if zona.plantilla_id else None
    resumen = simular_cobro(
        activacion=fecha or date.today(),
        meses_gratis=meses_gratis,
        precio_mensual=Decimal(str(plan.precio or 0)),
        plantilla=plantilla,
    )
    return {**resumen, "zona": zona.nombre, "plan": plan.nombre, "velocidad_bajada": plan.velocidad_bajada}

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
    Actualiza el nombre y la infraestructura (MikroTik, OLT y plantilla) de una zona.
    """
    service = ZoneService(db)
    try:
        # Nota: Asegúrate de tener el método editar_zona en tu servicio
        return await service.editar_zona(zona_id, datos)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
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
