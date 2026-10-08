from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List

# 🔥 1. IMPORTAMOS EL CACHÉ
from fastapi_cache.decorator import cache

from src.infrastructure.database import get_db
from src.domain import schemas 
from src.application.services.olt_service import OLTService
from src.application.services.snmp_service import SNMPMonitorService
from src.application.services.vinculacion_onu_service import VinculacionOnuService
from src.application.services.vsol_api_service import VsolApiService
from src.infrastructure.auth import role_required

router = APIRouter(prefix="/olts", tags=["OLTs"])


class OLTOptionResponse(BaseModel):
    id: int
    nombre: str

# ==========================================
# CATÁLOGO DE OLTs (¡CON CACHÉ!)
# ==========================================

@router.get("/", response_model=List[schemas.OLTResponse])
@cache(expire=300) # 🔥 Guardamos la lista de OLTs por 5 minutos
async def listar_olts(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    servicio = OLTService(db)
    return await servicio.obtener_todas()


@router.get("/opciones", response_model=List[OLTOptionResponse])
async def listar_opciones_olt(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(
        role_required(["admin", "supervisor", "tecnico"])
    ),
):
    servicio = OLTService(db)
    olts = await servicio.obtener_todas()
    return [{"id": olt.id, "nombre": olt.nombre} for olt in olts]


@router.get("/{olt_id}", response_model=schemas.OLTResponse)
@cache(expire=300) # 🔥 También podemos guardar el detalle de una OLT específica
async def obtener_olt(
    olt_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    servicio = OLTService(db)
    return await servicio.obtener_por_id(olt_id)

# ==========================================
# ESCRITURA (¡SIN CACHÉ!)
# ==========================================

@router.post("/", response_model=schemas.OLTResponse, status_code=status.HTTP_201_CREATED)
async def crear_olt(
    olt: schemas.OLTCreate,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin"])),
):
    servicio = OLTService(db)
    return await servicio.crear_olt(olt)

@router.put("/{olt_id}", response_model=schemas.OLTResponse)
async def actualizar_olt(
    olt_id: int,
    olt: schemas.OLTUpdate,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin"])),
):
    servicio = OLTService(db)
    return await servicio.actualizar_olt(olt_id, olt)

@router.delete("/{olt_id}", status_code=status.HTTP_204_NO_CONTENT)
async def eliminar_olt(
    olt_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin"])),
):
    servicio = OLTService(db)
    await servicio.eliminar_olt(olt_id)


# ==========================================
# MONITOREO SNMP EN VIVO (¡ESTRICTAMENTE SIN CACHÉ!)
# ==========================================

@router.get("/{olt_id}/monitoreo-vivo")
async def dashboard_olt_snmp(
    olt_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
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
async def diagnostico_individual_cliente(
    cliente_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(
        role_required(["admin", "supervisor", "tecnico"])
    ),
):
    """
    Diagnóstico en tiempo real de potencia óptica para un cliente específico.
    Llamado desde el Portal del Técnico.
    """
    # 🚫 NO USAMOS CACHÉ AQUÍ: Si el técnico está moviendo la fibra, necesita ver el cambio al instante.
    servicio = SNMPMonitorService(db)
    try:
        # Diagnóstico de solo lectura: cualquier técnico puede consultarlo en campo.
        resultado = await servicio.monitorear_cliente_individual(cliente_id)
        return {"status": "success", "data": resultado}
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error en diagnóstico: {str(e)}")
# ==========================================
# MONITOREO VSOL API JSON (SIN CACHÉ)
# ==========================================

@router.get("/{olt_id}/monitoreo-api")
async def monitoreo_olt_vsol_api(
    olt_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
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


@router.get("/{olt_id}/onus/{pon}/{onuid}/detalle")
async def detalle_onu_vsol_api(
    olt_id: int,
    pon: int,
    onuid: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    """Distancia, temperatura, voltaje, firmware e historial de caídas de una ONU."""
    try:
        return {"status": "success", "data": await VsolApiService(db).detalle_onu(olt_id, pon, onuid)}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"No se pudo consultar la ONU: {str(e)[:200]}")


class ReiniciarOnuRequest(BaseModel):
    # Serial que el usuario ve en pantalla: si la ONU de ese PON/número
    # cambió desde el último escaneo, no se reinicia otra por error.
    serial: str


@router.post("/{olt_id}/onus/{pon}/{onuid}/reiniciar")
async def reiniciar_onu_vsol_api(
    olt_id: int,
    pon: int,
    onuid: int,
    datos: ReiniciarOnuRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    """Reinicia (reboot, no reset de fábrica) una ONU por la API de la OLT."""
    try:
        return {"status": "success", "data": await VsolApiService(db).reiniciar_onu(olt_id, pon, onuid, datos.serial)}
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"No se pudo reiniciar la ONU: {str(e)[:200]}")


@router.post("/{olt_id}/onus/{pon}/{onuid}/eliminar")
async def eliminar_onu_vsol_api(
    olt_id: int,
    pon: int,
    onuid: int,
    datos: ReiniciarOnuRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin"])),
):
    """Borra de la OLT una ONU apagada sin cliente, para que cuadre con el sistema."""
    try:
        con_dueno = await VinculacionOnuService(db)._seriales_con_dueno()
        return {
            "status": "success",
            "data": await VsolApiService(db).eliminar_onu(olt_id, pon, onuid, datos.serial, con_dueno),
        }
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"No se pudo borrar la ONU: {str(e)[:200]}")


@router.get("/{olt_id}/onus-api")
async def listar_onus_vsol_api(
    olt_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
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


class VinculoOnu(BaseModel):
    identificador: str
    cliente_id: int


class VincularOnus(BaseModel):
    vinculos: List[VinculoOnu]


@router.get("/{olt_id}/vinculacion")
async def propuesta_de_vinculacion(
    olt_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    """ONU sin cliente en el sistema y el cliente que sugiere su descripción."""
    try:
        return await VinculacionOnuService(db).propuesta(olt_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"No se pudo leer la OLT: {str(e)[:200]}")


@router.post("/{olt_id}/vincular-onus")
async def vincular_onus(
    olt_id: int,
    datos: VincularOnus,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    """Guarda el serial en cada cliente elegido (y en su servicio)."""
    if not datos.vinculos:
        raise HTTPException(status_code=400, detail="No hay ONU por vincular")
    try:
        resultados = await VinculacionOnuService(db).vincular(
            olt_id, [v.model_dump() for v in datos.vinculos], current_user
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"No se pudo leer la OLT: {str(e)[:200]}")
    return {"resultados": resultados, "vinculadas": sum(1 for r in resultados if r["ok"])}


@router.get("/diagnostico-cliente-api/{cliente_id}")
async def diagnostico_cliente_vsol_api(
    cliente_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(
        role_required(["admin", "supervisor", "tecnico"])
    ),
):
    """
    Diagnóstico individual de cliente por API JSON VSOL.
    """
    servicio = VsolApiService(db)
    try:
        # Diagnóstico de solo lectura: cualquier técnico puede consultarlo en campo.
        resultado = await servicio.monitorear_cliente_individual_api(cliente_id)
        return {"status": "success", "data": resultado}
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error en diagnóstico VSOL API: {str(e)}")
