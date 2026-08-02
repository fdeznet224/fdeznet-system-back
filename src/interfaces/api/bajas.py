from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.services.baja_service import BajaService
from src.infrastructure.auth import role_required
from src.infrastructure.database import get_db
from src.infrastructure.models import BajaServicioModel, UsuarioModel


router = APIRouter(prefix="/bajas", tags=["Bajas de servicio"])


class BajaCrear(BaseModel):
    servicio_id: Optional[int] = Field(default=None, gt=0)
    motivo: str = Field(min_length=5, max_length=500)
    observaciones: Optional[str] = Field(default=None, max_length=2000)
    tecnico_id: Optional[int] = Field(default=None, gt=0)
    fecha_programada: Optional[datetime] = None


class AsignarTecnico(BaseModel):
    tecnico_id: int = Field(gt=0)


class ConfirmarRetiro(BaseModel):
    condicion: Literal["funcional", "danada", "incompleta", "perdida"]
    observaciones: Optional[str] = Field(default=None, max_length=2000)


def serializar_baja(baja: BajaServicioModel):
    return {
        "id": baja.id,
        "cliente_id": baja.cliente_id,
        "cliente": (
            {
                "id": baja.cliente.id,
                "nombre": baja.cliente.nombre,
                "cedula": baja.cliente.cedula,
                "telefono": baja.cliente.telefono,
                "direccion": baja.cliente.direccion,
                "estado": baja.cliente.estado,
            }
            if baja.cliente
            else None
        ),
        "servicio_id": baja.servicio_id,
        "orden_retiro_id": baja.orden_retiro_id,
        "onu": (
            {
                "id": baja.onu.id,
                "identificador": baja.onu.identificador,
                "modelo": baja.onu.modelo,
                "estado": baja.onu.estado,
            }
            if baja.onu
            else None
        ),
        "solicitada_por": (
            {
                "id": baja.solicitada_por.id,
                "nombre": baja.solicitada_por.nombre_completo,
            }
            if baja.solicitada_por
            else None
        ),
        "tecnico": (
            {
                "id": baja.tecnico.id,
                "nombre": baja.tecnico.nombre_completo,
                "usuario": baja.tecnico.usuario,
            }
            if baja.tecnico
            else None
        ),
        "estado": baja.estado,
        "motivo": baja.motivo,
        "observaciones": baja.observaciones,
        "condicion_equipo": baja.condicion_equipo,
        "mikrotik_estado": baja.mikrotik_estado,
        "mikrotik_error": baja.mikrotik_error,
        "snapshot": {
            "ip": baja.ip_snapshot,
            "caja_nap_id": baja.caja_nap_id_snapshot,
            "puerto_nap": baja.puerto_nap_snapshot,
            "estado_servicio": baja.servicio_estado_snapshot,
            "proxima_facturacion": baja.proxima_facturacion_snapshot,
        },
        "solicitada_en": baja.solicitada_en,
        "recuperada_en": baja.recuperada_en,
        "cancelada_en": baja.cancelada_en,
    }


def manejar_error(exc: Exception):
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if isinstance(exc, RuntimeError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/clientes/{cliente_id}", status_code=201)
async def crear_baja(
    cliente_id: int,
    datos: BajaCrear,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    try:
        baja = await BajaService(db).crear(
            cliente_id=cliente_id,
            motivo=datos.motivo,
            usuario=current_user,
            tecnico_id=datos.tecnico_id,
            fecha_programada=datos.fecha_programada,
            observaciones=datos.observaciones,
            servicio_id=datos.servicio_id,
        )
        return serializar_baja(baja)
    except (ValueError, PermissionError, RuntimeError) as exc:
        await db.rollback()
        manejar_error(exc)


@router.get("/")
async def listar_bajas(
    estado: Optional[str] = None,
    tecnico_id: Optional[int] = None,
    cliente_id: Optional[int] = None,
    limite: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(
        role_required(["admin", "supervisor", "tecnico"])
    ),
):
    bajas = await BajaService(db).listar(
        usuario=current_user,
        estado=estado,
        tecnico_id=tecnico_id,
        cliente_id=cliente_id,
        limite=limite,
    )
    return [serializar_baja(baja) for baja in bajas]


@router.get("/tecnicos/disponibles")
async def listar_tecnicos_disponibles(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    tecnicos = (
        await db.execute(
            select(UsuarioModel)
            .where(
                UsuarioModel.rol == "tecnico",
                UsuarioModel.activo.is_(True),
            )
            .order_by(UsuarioModel.nombre_completo, UsuarioModel.usuario)
        )
    ).scalars().all()
    return [
        {
            "id": tecnico.id,
            "nombre_completo": tecnico.nombre_completo,
            "usuario": tecnico.usuario,
            "rol": tecnico.rol,
            "activo": tecnico.activo,
        }
        for tecnico in tecnicos
    ]


@router.get("/{baja_id}")
async def obtener_baja(
    baja_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(
        role_required(["admin", "supervisor", "tecnico"])
    ),
):
    try:
        baja = await BajaService(db).obtener(baja_id, current_user)
        return serializar_baja(baja)
    except (ValueError, PermissionError) as exc:
        manejar_error(exc)


@router.post("/{baja_id}/asignar")
async def asignar_tecnico(
    baja_id: int,
    datos: AsignarTecnico,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    try:
        baja = await BajaService(db).asignar_tecnico(
            baja_id,
            datos.tecnico_id,
            current_user,
        )
        return serializar_baja(baja)
    except (ValueError, PermissionError) as exc:
        await db.rollback()
        manejar_error(exc)


@router.post("/{baja_id}/confirmar-retiro")
async def confirmar_retiro(
    baja_id: int,
    datos: ConfirmarRetiro,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(
        role_required(["admin", "supervisor", "tecnico"])
    ),
):
    try:
        baja = await BajaService(db).confirmar_retiro(
            baja_id,
            datos.condicion,
            current_user,
            datos.observaciones,
        )
        return serializar_baja(baja)
    except (ValueError, PermissionError) as exc:
        await db.rollback()
        manejar_error(exc)


@router.post("/ordenes/{orden_id}/confirmar-retiro")
async def confirmar_retiro_por_orden(
    orden_id: int,
    datos: ConfirmarRetiro,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(
        role_required(["admin", "supervisor", "tecnico"])
    ),
):
    baja_id = (
        await db.execute(
            select(BajaServicioModel.id).where(
                BajaServicioModel.orden_retiro_id == orden_id
            )
        )
    ).scalar_one_or_none()
    if not baja_id:
        raise HTTPException(
            status_code=404,
            detail="La orden no pertenece a una baja registrada",
        )
    try:
        baja = await BajaService(db).confirmar_retiro(
            baja_id,
            datos.condicion,
            current_user,
            datos.observaciones,
        )
        return serializar_baja(baja)
    except (ValueError, PermissionError) as exc:
        await db.rollback()
        manejar_error(exc)


@router.post("/{baja_id}/reintentar-mikrotik")
async def reintentar_mikrotik(
    baja_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    try:
        await BajaService(db).obtener(baja_id, current_user)
        baja = await BajaService(db).sincronizar_mikrotik(baja_id)
        return serializar_baja(
            await BajaService(db).obtener(baja.id, current_user)
        )
    except (ValueError, PermissionError) as exc:
        await db.rollback()
        manejar_error(exc)


@router.post("/{baja_id}/cancelar-reactivar")
async def cancelar_y_reactivar(
    baja_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    try:
        baja = await BajaService(db).cancelar_y_reactivar(
            baja_id,
            current_user,
        )
        return serializar_baja(baja)
    except (ValueError, PermissionError, RuntimeError) as exc:
        await db.rollback()
        manejar_error(exc)
