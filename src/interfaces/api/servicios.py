from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.services.access_control_service import (
    verificar_acceso_cliente,
)
from src.application.services.subscription_service import (
    SubscriptionService,
)
from src.domain.schemas import (
    ServicioActivacion,
    ServicioCreate,
    ServicioEstadoUpdate,
    ServicioPlanUpdate,
    ServicioPlanUpdateResponse,
    ServicioResponse,
    ServicioUpdate,
)
from src.infrastructure.auth import role_required
from src.infrastructure.database import get_db


router = APIRouter(prefix="/servicios", tags=["Servicios por domicilio"])


@router.get(
    "/cliente/{cliente_id}",
    response_model=list[ServicioResponse],
)
async def listar_servicios_cliente(
    cliente_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(
        role_required(["admin", "supervisor", "cajero", "tecnico"])
    ),
):
    try:
        await verificar_acceso_cliente(db, current_user, cliente_id)
        return await SubscriptionService(db).listar_cliente(cliente_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.get("/{servicio_id}", response_model=ServicioResponse)
async def obtener_servicio(
    servicio_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(
        role_required(["admin", "supervisor", "cajero", "tecnico"])
    ),
):
    try:
        servicio = await SubscriptionService(db).obtener(servicio_id)
        await verificar_acceso_cliente(
            db,
            current_user,
            servicio.cliente_id,
        )
        return servicio
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/", response_model=ServicioResponse, status_code=201)
async def crear_servicio(
    datos: ServicioCreate,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    try:
        return await SubscriptionService(db).crear(
            datos,
            current_user.id,
        )
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.patch("/{servicio_id}", response_model=ServicioResponse)
async def actualizar_servicio(
    servicio_id: int,
    datos: ServicioUpdate,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    try:
        return await SubscriptionService(db).actualizar(
            servicio_id,
            datos,
        )
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/{servicio_id}/activar",
    response_model=ServicioResponse,
)
async def activar_servicio(
    servicio_id: int,
    datos: ServicioActivacion,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(
        role_required(["admin", "supervisor", "tecnico"])
    ),
):
    try:
        servicio = await SubscriptionService(db).obtener(servicio_id)
        await verificar_acceso_cliente(
            db,
            current_user,
            servicio.cliente_id,
        )
        return await SubscriptionService(db).activar(
            servicio_id,
            datos,
            current_user.id,
        )
    except PermissionError as exc:
        await db.rollback()
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.put(
    "/{servicio_id}/estado",
    response_model=ServicioResponse,
)
async def cambiar_estado_servicio(
    servicio_id: int,
    datos: ServicioEstadoUpdate,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    try:
        return await SubscriptionService(db).cambiar_estado(
            servicio_id,
            datos.estado,
        )
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        await db.rollback()
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.put(
    "/{servicio_id}/plan",
    response_model=ServicioPlanUpdateResponse,
)
async def cambiar_plan_servicio(
    servicio_id: int,
    datos: ServicioPlanUpdate,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    try:
        servicio = await SubscriptionService(db).obtener(servicio_id)
        await verificar_acceso_cliente(
            db,
            current_user,
            servicio.cliente_id,
        )
        return await SubscriptionService(db).cambiar_plan(
            servicio_id,
            datos,
        )
    except PermissionError as exc:
        await db.rollback()
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
