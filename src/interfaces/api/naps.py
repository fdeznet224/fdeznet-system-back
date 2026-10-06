from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, Field

from src.infrastructure.database import get_db
from src.domain.schemas import CajaNapCreate, CajaNapResponse
from src.application.services.nap_service import NapService
from src.application.services.aviso_averia_service import MENSAJE_SUGERIDO, encolar_aviso
from src.infrastructure.auth import role_required

router = APIRouter(prefix="/infraestructura", tags=["Cajas NAP y Fibra"])


class PuertoNapOcupadoResponse(BaseModel):
    id: int
    nombre: str
    puerto_nap: int
    cedula: Optional[str] = None

class NapSugeridaResponse(BaseModel):
    id: int
    nombre: str
    ubicacion: Optional[str] = None
    zona_id: Optional[int] = None
    zona_nombre: Optional[str] = None
    olt_id: Optional[int] = None
    olt_nombre: Optional[str] = None
    capacidad: Optional[int] = None
    puertos_libres: int
    distancia_m: int
    posicion_estimada: bool

# ==========================================
# CATÁLOGO DE NAPs
# ==========================================
@router.get("/naps", response_model=List[CajaNapResponse])
async def listar_cajas_nap(
    zona_id: Optional[int] = Query(default=None, ge=1),
    router_id: Optional[int] = Query(default=None, ge=1),
    olt_id: Optional[int] = Query(default=None, ge=1),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(
        role_required(["admin", "supervisor", "tecnico"])
    ),
):
    """Obtiene NAPs compatibles con la zona, el router y/o la OLT."""
    service = NapService(db)
    return await service.listar_naps(
        zona_id=zona_id,
        router_id=router_id,
        olt_id=olt_id,
    )


@router.get("/naps/cercanas", response_model=List[NapSugeridaResponse])
async def sugerir_cajas_nap(
    latitud: float = Query(ge=-90, le=90),
    longitud: float = Query(ge=-180, le=180),
    zona_id: Optional[int] = Query(default=None, ge=1),
    olt_id: Optional[int] = Query(default=None, ge=1),
    limite: int = Query(default=3, ge=1, le=10),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(
        role_required(["admin", "supervisor", "tecnico"])
    ),
):
    """Cajas NAP más cercanas al domicilio, para sugerir cuál usar."""
    return await NapService(db).sugerir_naps(
        latitud=latitud,
        longitud=longitud,
        zona_id=zona_id,
        olt_id=olt_id,
        limite=limite,
    )


@router.get("/naps/sin-asignar")
async def servicios_sin_nap(
    zona_id: Optional[int] = Query(default=None, ge=1),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    """Servicios vigentes sin caja NAP, con las cajas más cercanas por GPS."""
    return await NapService(db).servicios_sin_nap(zona_id=zona_id)


class AsignarNapRequest(BaseModel):
    servicio_id: int = Field(gt=0)
    caja_nap_id: int = Field(gt=0)
    puerto_nap: Optional[int] = Field(default=None, ge=1, le=128)


@router.post("/naps/asignar")
async def asignar_nap(
    datos: AsignarNapRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    try:
        return await NapService(db).asignar_nap_servicio(
            datos.servicio_id, datos.caja_nap_id, current_user.id, datos.puerto_nap
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


# ==========================================
# AVISOS DE AVERÍA
# ==========================================
class AvisoAveriaRequest(BaseModel):
    caja_nap_id: Optional[int] = Field(default=None, gt=0)
    olt_id: Optional[int] = Field(default=None, gt=0)
    puerto_olt: Optional[int] = Field(default=None, ge=0, le=128)
    zona_id: Optional[int] = Field(default=None, gt=0)
    mensaje: str = Field(default=MENSAJE_SUGERIDO, min_length=10, max_length=1000)


async def _afectados(db: AsyncSession, datos: AvisoAveriaRequest):
    try:
        return await NapService(db).afectados_por_averia(
            caja_nap_id=datos.caja_nap_id,
            olt_id=datos.olt_id,
            puerto_olt=datos.puerto_olt,
            zona_id=datos.zona_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/avisos-averia/vista-previa")
async def vista_previa_aviso(
    datos: AvisoAveriaRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    """A quién le llegaría el aviso, antes de mandarlo."""
    afectados = await _afectados(db, datos)
    return {
        "total": len(afectados),
        "clientes": [a["nombre"] for a in afectados[:50]],
        "mensaje_sugerido": MENSAJE_SUGERIDO,
    }


@router.post("/avisos-averia")
async def enviar_aviso_averia(
    datos: AvisoAveriaRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    afectados = await _afectados(db, datos)
    if not afectados:
        raise HTTPException(status_code=404, detail="No hay clientes con teléfono afectados por esa falla")
    return await encolar_aviso(db, afectados, datos.mensaje, current_user.id)


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
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.put("/naps/{id}", response_model=CajaNapResponse)
async def actualizar_caja_nap(
    id: int,
    data: CajaNapCreate,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    service = NapService(db)
    try:
        return await service.actualizar_nap(id, data)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

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
