from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.services.orden_service import OrdenService
from src.application.services.support_service import SupportService
from src.infrastructure.auth import role_required
from src.infrastructure.database import get_db
from src.interfaces.api.ordenes import serializar_orden


router = APIRouter(prefix="/soporte", tags=["Soporte técnico"])


class IncidenciaCrear(BaseModel):
    cliente_id: int
    categoria: str = Field(
        pattern=(
            r"^(sin_internet|lentitud|potencia_baja|router_wifi|"
            r"cable_roto|cambio_domicilio|otro)$"
        )
    )
    descripcion: str = Field(min_length=5, max_length=2000)
    tecnico_id: Optional[int] = None
    prioridad: Optional[str] = Field(
        default=None,
        pattern=r"^(baja|normal|alta|urgente)$",
    )
    fecha_programada: Optional[datetime] = None
    canal_reporte: str = Field(
        default="panel",
        pattern=r"^(panel|telefono|whatsapp|presencial|monitoreo)$",
    )


class ResolverIncidencia(BaseModel):
    solucion: str = Field(min_length=5, max_length=4000)
    comentario: Optional[str] = Field(default=None, max_length=1000)
    version: int = Field(ge=1)


def serializar_diagnostico(item):
    return {
        "id": item.id,
        "orden_id": item.orden_id,
        "cliente_id": item.cliente_id,
        "resultado": item.resultado,
        "codigo_sugerencia": item.codigo_sugerencia,
        "sugerencia": item.sugerencia,
        "mikrotik": {
            "disponible": item.mikrotik_disponible,
            "pppoe_online": item.pppoe_online,
            "ip_actual": item.ip_actual,
            "uptime": item.uptime,
            "mac_reportada": item.mac_reportada,
            "ping_estado": item.ping_estado,
            "perdida_paquetes_porcentaje": (
                item.perdida_paquetes_porcentaje
            ),
            "trafico_subida_bps": item.trafico_subida_bps,
            "trafico_bajada_bps": item.trafico_bajada_bps,
        },
        "olt": {
            "disponible": item.olt_disponible,
            "onu_online": item.onu_online,
            "potencia_rx_dbm": item.potencia_rx_dbm,
            "potencia_tx_dbm": item.potencia_tx_dbm,
            "origen": item.origen_olt,
        },
        "errores": item.errores,
        "fecha": item.fecha,
        "ejecutado_por": (
            {
                "id": item.ejecutado_por.id,
                "nombre": item.ejecutado_por.nombre_completo,
            }
            if item.ejecutado_por
            else None
        ),
    }


def manejar_error(exc):
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if isinstance(exc, RuntimeError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/incidencias", status_code=201)
async def crear_incidencia(
    datos: IncidenciaCrear,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(
        role_required(["admin", "supervisor", "cajero", "tecnico"])
    ),
):
    tecnico_id = datos.tecnico_id
    if current_user.rol == "tecnico":
        if tecnico_id and tecnico_id != current_user.id:
            raise HTTPException(
                status_code=403,
                detail="Un técnico no puede asignar la incidencia a otra persona",
            )
        tecnico_id = current_user.id
    try:
        orden = await SupportService(db).crear_incidencia(
            cliente_id=datos.cliente_id,
            categoria=datos.categoria,
            descripcion=datos.descripcion,
            usuario=current_user,
            tecnico_id=tecnico_id,
            prioridad=datos.prioridad,
            fecha_programada=datos.fecha_programada,
            canal_reporte=datos.canal_reporte,
        )
        return serializar_orden(orden)
    except (ValueError, PermissionError, RuntimeError) as exc:
        await db.rollback()
        manejar_error(exc)


@router.get("/bandeja")
async def bandeja_soporte(
    estado: Optional[str] = None,
    categoria: Optional[str] = None,
    prioridad: Optional[str] = None,
    solo_vencidas: bool = False,
    limite: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor", "tecnico"])),
):
    try:
        ordenes = await SupportService(db).bandeja(
            current_user,
            estado=estado,
            categoria=categoria,
            prioridad=prioridad,
            solo_vencidas=solo_vencidas,
            limite=limite,
        )
        return [serializar_orden(orden) for orden in ordenes]
    except ValueError as exc:
        manejar_error(exc)


@router.get("/metricas")
async def metricas_soporte(
    desde: date,
    hasta: date,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    if hasta < desde:
        raise HTTPException(400, "La fecha final no puede ser anterior")
    return await SupportService(db).metricas(desde, hasta)


@router.get("/incidencias/{orden_id}")
async def obtener_incidencia(
    orden_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor", "tecnico"])),
):
    try:
        orden = await OrdenService(db).obtener(orden_id, current_user)
        if not orden.categoria_soporte:
            raise ValueError("La orden no corresponde a una incidencia")
        return serializar_orden(orden)
    except (ValueError, PermissionError) as exc:
        manejar_error(exc)


@router.post("/incidencias/{orden_id}/diagnosticar")
async def ejecutar_diagnostico(
    orden_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor", "tecnico"])),
):
    try:
        diagnostico = await SupportService(db).ejecutar_diagnostico(
            orden_id,
            current_user,
        )
        return serializar_diagnostico(diagnostico)
    except (ValueError, PermissionError) as exc:
        await db.rollback()
        manejar_error(exc)


@router.get("/incidencias/{orden_id}/diagnosticos")
async def historial_diagnosticos(
    orden_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor", "tecnico"])),
):
    try:
        items = await SupportService(db).listar_diagnosticos(
            orden_id,
            current_user,
        )
        return [serializar_diagnostico(item) for item in items]
    except (ValueError, PermissionError) as exc:
        manejar_error(exc)


@router.post("/incidencias/{orden_id}/resolver")
async def resolver_incidencia(
    orden_id: int,
    datos: ResolverIncidencia,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor", "tecnico"])),
):
    service = OrdenService(db)
    try:
        orden = await service.obtener(orden_id, current_user)
        if not orden.categoria_soporte:
            raise ValueError("La orden no corresponde a una incidencia")
        if orden.estado != "trabajando":
            raise ValueError(
                "La incidencia debe estar en estado trabajando para resolverla"
            )
        if orden.version != datos.version:
            raise RuntimeError(
                "La orden cambió en otro dispositivo; actualiza antes de continuar"
            )
        orden.solucion = datos.solucion.strip()
        await db.flush()
        orden = await service.cambiar_estado(
            orden.id,
            "terminada",
            datos.comentario or "Incidencia resuelta",
            datos.version,
            current_user,
        )
        return serializar_orden(orden)
    except (ValueError, PermissionError, RuntimeError) as exc:
        await db.rollback()
        manejar_error(exc)
