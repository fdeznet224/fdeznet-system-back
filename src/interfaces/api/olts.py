from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List

from src.infrastructure.database import get_db
from src.domain import schemas 
from src.application.services.olt_service import OLTService
from src.application.services.snmp_service import SNMPMonitorService

router = APIRouter(prefix="/olts", tags=["OLTs"])

@router.get("/", response_model=List[schemas.OLTResponse])
async def listar_olts(db: AsyncSession = Depends(get_db)):
    servicio = OLTService(db)
    return await servicio.obtener_todas()

@router.post("/", response_model=schemas.OLTResponse, status_code=status.HTTP_201_CREATED)
async def crear_olt(olt: schemas.OLTCreate, db: AsyncSession = Depends(get_db)):
    servicio = OLTService(db)
    return await servicio.crear_olt(olt)

@router.get("/{olt_id}", response_model=schemas.OLTResponse)
async def obtener_olt(olt_id: int, db: AsyncSession = Depends(get_db)):
    servicio = OLTService(db)
    return await servicio.obtener_por_id(olt_id)

@router.put("/{olt_id}", response_model=schemas.OLTResponse)
async def actualizar_olt(olt_id: int, olt: schemas.OLTUpdate, db: AsyncSession = Depends(get_db)):
    servicio = OLTService(db)
    return await servicio.actualizar_olt(olt_id, olt)

@router.delete("/{olt_id}", status_code=status.HTTP_204_NO_CONTENT)
async def eliminar_olt(olt_id: int, db: AsyncSession = Depends(get_db)):
    servicio = OLTService(db)
    await servicio.eliminar_olt(olt_id)


@router.get("/{olt_id}/monitoreo-vivo")
async def dashboard_olt_snmp(olt_id: int, db: AsyncSession = Depends(get_db)):
    """
    Escanea toda la OLT y cruza TODO con la Base de Datos.
    Ideal para la vista de "Mapa de Fibra" general.
    """
    servicio = SNMPMonitorService(db)
    try:
        resultados = await servicio.monitorear_olt(olt_id)
        return {"status": "success", "data": resultados}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error consultando la OLT: {str(e)}") 
    

@router.get("/diagnostico-cliente/{cliente_id}")
async def diagnostico_individual_cliente(cliente_id: int, db: AsyncSession = Depends(get_db)):
    """
    Diagnóstico en tiempo real de potencia óptica para un cliente específico.
    Llamado desde el Portal del Técnico.
    """
    servicio = SNMPMonitorService(db)
    try:
        resultado = await servicio.monitorear_cliente_individual(cliente_id)
        return {"status": "success", "data": resultado}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error en diagnóstico: {str(e)}")