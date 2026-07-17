from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List

# 🔥 1. IMPORTAMOS EL CACHÉ
from fastapi_cache.decorator import cache

from src.infrastructure.database import get_db
from src.domain import schemas 
from src.application.services.olt_service import OLTService
from src.application.services.snmp_service import SNMPMonitorService
from src.application.services.vsol_api_service import VsolApiService

router = APIRouter(prefix="/olts", tags=["OLTs"])

# ==========================================
# CATÁLOGO DE OLTs (¡CON CACHÉ!)
# ==========================================

@router.get("/", response_model=List[schemas.OLTResponse])
@cache(expire=300) # 🔥 Guardamos la lista de OLTs por 5 minutos
async def listar_olts(db: AsyncSession = Depends(get_db)):
    servicio = OLTService(db)
    return await servicio.obtener_todas()

@router.get("/{olt_id}", response_model=schemas.OLTResponse)
@cache(expire=300) # 🔥 También podemos guardar el detalle de una OLT específica
async def obtener_olt(olt_id: int, db: AsyncSession = Depends(get_db)):
    servicio = OLTService(db)
    return await servicio.obtener_por_id(olt_id)

# ==========================================
# ESCRITURA (¡SIN CACHÉ!)
# ==========================================

@router.post("/", response_model=schemas.OLTResponse, status_code=status.HTTP_201_CREATED)
async def crear_olt(olt: schemas.OLTCreate, db: AsyncSession = Depends(get_db)):
    servicio = OLTService(db)
    return await servicio.crear_olt(olt)

@router.put("/{olt_id}", response_model=schemas.OLTResponse)
async def actualizar_olt(olt_id: int, olt: schemas.OLTUpdate, db: AsyncSession = Depends(get_db)):
    servicio = OLTService(db)
    return await servicio.actualizar_olt(olt_id, olt)

@router.delete("/{olt_id}", status_code=status.HTTP_204_NO_CONTENT)
async def eliminar_olt(olt_id: int, db: AsyncSession = Depends(get_db)):
    servicio = OLTService(db)
    await servicio.eliminar_olt(olt_id)


# ==========================================
# MONITOREO SNMP EN VIVO (¡ESTRICTAMENTE SIN CACHÉ!)
# ==========================================

@router.get("/{olt_id}/monitoreo-vivo")
async def dashboard_olt_snmp(olt_id: int, db: AsyncSession = Depends(get_db)):
    """
    Escanea toda la OLT y cruza TODO con la Base de Datos.
    Ideal para la vista de "Mapa de Fibra" general.
    """
    # 🚫 NO USAMOS CACHÉ AQUÍ: Necesitamos los datos frescos de la OLT en tiempo real
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
    # 🚫 NO USAMOS CACHÉ AQUÍ: Si el técnico está moviendo la fibra, necesita ver el cambio al instante.
    servicio = SNMPMonitorService(db)
    try:
        resultado = await servicio.monitorear_cliente_individual(cliente_id)
        return {"status": "success", "data": resultado}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error en diagnóstico: {str(e)}")
# ==========================================
# MONITOREO VSOL API JSON (SIN CACHÉ)
# ==========================================

@router.get("/{olt_id}/monitoreo-api")
async def monitoreo_olt_vsol_api(olt_id: int, db: AsyncSession = Depends(get_db)):
    """
    Escanea toda la OLT usando la API JSON web de VSOL.
    No reemplaza SNMP todavía; es la nueva ruta de lectura para validar API.
    """
    servicio = VsolApiService(db)
    try:
        resultados = await servicio.monitorear_olt_api(olt_id)
        return {"status": "success", "data": resultados}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error consultando VSOL API: {str(e)}")


@router.get("/{olt_id}/onus-api")
async def listar_onus_vsol_api(olt_id: int, db: AsyncSession = Depends(get_db)):
    """
    Lista ONUs unificadas desde authinfo + opticalinfo + statusinfo.
    """
    servicio = VsolApiService(db)
    try:
        resultados = await servicio.listar_onus_unificadas(olt_id)
        return {"status": "success", "data": resultados}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error consultando VSOL API: {str(e)}")


@router.get("/diagnostico-cliente-api/{cliente_id}")
async def diagnostico_cliente_vsol_api(cliente_id: int, db: AsyncSession = Depends(get_db)):
    """
    Diagnóstico individual de cliente por API JSON VSOL.
    """
    servicio = VsolApiService(db)
    try:
        resultado = await servicio.monitorear_cliente_individual_api(cliente_id)
        return {"status": "success", "data": resultado}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error en diagnóstico VSOL API: {str(e)}")
