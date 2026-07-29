from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.application.services.ftth_service import FTTHService
from src.infrastructure.auth import role_required
from src.infrastructure.database import get_db
from src.infrastructure.models import (
    ClienteModel,
    HistorialEquipoModel,
    InventarioONUModel,
    LecturaOpticaModel,
    OrdenServicioModel,
)


router = APIRouter(prefix="/ftth", tags=["Control FTTH"])


class EstadoPuertoRequest(BaseModel):
    estado: str = Field(pattern=r"^(libre|reservado|danado)$")
    observaciones: Optional[str] = Field(default=None, max_length=500)
    orden_id: Optional[int] = None


class AsignacionPuertoRequest(BaseModel):
    cliente_id: int
    potencia_instalacion_dbm: Optional[Decimal] = Field(
        default=None,
        ge=-50,
        le=10,
        max_digits=6,
        decimal_places=2,
    )
    orden_id: Optional[int] = None


class LecturaOpticaRequest(BaseModel):
    potencia_rx_dbm: Decimal = Field(
        ge=-50,
        le=10,
        max_digits=6,
        decimal_places=2,
    )
    potencia_tx_dbm: Optional[Decimal] = Field(
        default=None,
        ge=-50,
        le=20,
        max_digits=6,
        decimal_places=2,
    )
    orden_id: Optional[int] = None
    origen: str = Field(default="manual", pattern=r"^(manual|snmp|olt_api)$")
    observaciones: Optional[str] = Field(default=None, max_length=500)


def puerto_response(puerto):
    return {
        "id": puerto.id,
        "caja_nap_id": puerto.caja_nap_id,
        "numero": puerto.numero,
        "estado": puerto.estado,
        "cliente_id": puerto.cliente_id,
        "orden_id": puerto.orden_id,
        "potencia_instalacion_dbm": puerto.potencia_instalacion_dbm,
        "observaciones": puerto.observaciones,
        "actualizado_por_id": puerto.actualizado_por_id,
        "updated_at": puerto.updated_at,
    }


async def validar_orden_del_tecnico(
    db: AsyncSession,
    current_user,
    orden_id: Optional[int],
    cliente_id: int,
):
    if current_user.rol != "tecnico":
        return
    if not orden_id:
        raise HTTPException(403, "El técnico debe indicar su orden de servicio")
    orden = await db.get(OrdenServicioModel, orden_id)
    if (
        not orden
        or orden.tecnico_id != current_user.id
        or orden.cliente_id != cliente_id
        or orden.estado in {"terminada", "cancelada"}
    ):
        raise HTTPException(403, "La orden no está asignada a este técnico")


@router.get("/naps/{caja_nap_id}/puertos")
async def listar_puertos(
    caja_nap_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(
        role_required(["admin", "supervisor", "tecnico"])
    ),
):
    try:
        puertos = await FTTHService(db).listar_puertos(caja_nap_id)
        await db.commit()
        return [puerto_response(puerto) for puerto in puertos]
    except ValueError as error:
        raise HTTPException(404, str(error))


@router.patch("/naps/{caja_nap_id}/puertos/{numero}")
async def cambiar_estado_puerto(
    caja_nap_id: int,
    numero: int,
    datos: EstadoPuertoRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    try:
        puerto = await FTTHService(db).cambiar_estado_puerto(
            caja_nap_id,
            numero,
            datos.estado,
            current_user.id,
            datos.observaciones,
            datos.orden_id,
        )
        await db.commit()
        return puerto_response(puerto)
    except ValueError as error:
        raise HTTPException(409, str(error))


@router.post("/naps/{caja_nap_id}/puertos/{numero}/asignar")
async def asignar_puerto(
    caja_nap_id: int,
    numero: int,
    datos: AsignacionPuertoRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(
        role_required(["admin", "supervisor", "tecnico"])
    ),
):
    cliente = await db.get(ClienteModel, datos.cliente_id)
    if not cliente:
        raise HTTPException(404, "Cliente no encontrado")
    await validar_orden_del_tecnico(
        db,
        current_user,
        datos.orden_id,
        cliente.id,
    )
    try:
        puerto = await FTTHService(db).asignar_puerto(
            cliente,
            caja_nap_id,
            numero,
            current_user.id,
            datos.orden_id,
            datos.potencia_instalacion_dbm,
        )
        await db.commit()
        return puerto_response(puerto)
    except ValueError as error:
        await db.rollback()
        raise HTTPException(409, str(error))


@router.post("/clientes/{cliente_id}/lecturas-opticas", status_code=201)
async def registrar_lectura(
    cliente_id: int,
    datos: LecturaOpticaRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(
        role_required(["admin", "supervisor", "tecnico"])
    ),
):
    cliente = await db.get(ClienteModel, cliente_id)
    if not cliente:
        raise HTTPException(404, "Cliente no encontrado")
    await validar_orden_del_tecnico(
        db,
        current_user,
        datos.orden_id,
        cliente.id,
    )
    try:
        lectura = await FTTHService(db).registrar_lectura_optica(
            cliente,
            datos.potencia_rx_dbm,
            current_user.id,
            datos.potencia_tx_dbm,
            datos.orden_id,
            datos.origen,
            datos.observaciones,
        )
        await db.commit()
        return {
            "id": lectura.id,
            "cliente_id": lectura.cliente_id,
            "onu_id": lectura.onu_id,
            "orden_id": lectura.orden_id,
            "potencia_rx_dbm": lectura.potencia_rx_dbm,
            "potencia_tx_dbm": lectura.potencia_tx_dbm,
            "origen": lectura.origen,
            "fecha": lectura.fecha,
        }
    except ValueError as error:
        raise HTTPException(400, str(error))


@router.get("/clientes/{cliente_id}/lecturas-opticas")
async def listar_lecturas(
    cliente_id: int,
    limite: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(
        role_required(["admin", "supervisor", "tecnico"])
    ),
):
    resultado = await db.execute(
        select(LecturaOpticaModel)
        .where(LecturaOpticaModel.cliente_id == cliente_id)
        .order_by(LecturaOpticaModel.fecha.desc())
        .limit(limite)
    )
    return [
        {
            "id": item.id,
            "onu_id": item.onu_id,
            "orden_id": item.orden_id,
            "tecnico_id": item.tecnico_id,
            "potencia_rx_dbm": item.potencia_rx_dbm,
            "potencia_tx_dbm": item.potencia_tx_dbm,
            "origen": item.origen,
            "observaciones": item.observaciones,
            "fecha": item.fecha,
        }
        for item in resultado.scalars().all()
    ]


@router.get("/onus/{onu_id}/historial")
async def historial_onu(
    onu_id: int,
    limite: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(
        role_required(["admin", "supervisor", "tecnico"])
    ),
):
    onu = await db.get(InventarioONUModel, onu_id)
    if not onu:
        raise HTTPException(404, "ONU no encontrada")
    resultado = await db.execute(
        select(HistorialEquipoModel)
        .options(selectinload(HistorialEquipoModel.cliente))
        .where(HistorialEquipoModel.onu_id == onu_id)
        .order_by(HistorialEquipoModel.fecha.desc())
        .limit(limite)
    )
    return {
        "onu": {
            "id": onu.id,
            "identificador": onu.identificador,
            "estado": onu.estado,
        },
        "movimientos": [
            {
                "id": item.id,
                "cliente_id": item.cliente_id,
                "cliente_nombre": item.cliente.nombre if item.cliente else None,
                "tecnico_id": item.tecnico_id,
                "orden_id": item.orden_id,
                "tipo_movimiento": item.tipo_movimiento,
                "estado_anterior": item.estado_anterior,
                "estado_nuevo": item.estado_nuevo,
                "condicion": item.condicion,
                "motivo": item.motivo,
                "potencia_optica_dbm": item.potencia_optica_dbm,
                "fecha": item.fecha,
            }
            for item in resultado.scalars().all()
        ],
    }
