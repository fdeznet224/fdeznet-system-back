from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.models import (
    ClienteModel,
    InventarioONUModel,
    OrdenServicioModel,
    ServicioModel,
    UsuarioModel,
)


ESTADOS_ORDEN_ABIERTA = {
    "pendiente",
    "asignada",
    "en_camino",
    "trabajando",
}


def filtro_clientes_del_tecnico(tecnico_id: int):
    """Limita clientes a trabajos o equipos asignados al técnico autenticado."""
    orden_asignada = (
        select(OrdenServicioModel.id)
        .where(
            OrdenServicioModel.cliente_id == ClienteModel.id,
            OrdenServicioModel.tecnico_id == tecnico_id,
            OrdenServicioModel.estado.in_(ESTADOS_ORDEN_ABIERTA),
        )
        .exists()
    )
    onu_para_retiro = ClienteModel.onu_asignada.has(
        InventarioONUModel.tecnico_id == tecnico_id
    )
    servicio_asignado = (
        select(ServicioModel.id)
        .where(
            ServicioModel.cliente_id == ClienteModel.id,
            ServicioModel.tecnico_id == tecnico_id,
            ServicioModel.estado != "cancelado",
        )
        .exists()
    )
    return or_(
        ClienteModel.tecnico_id == tecnico_id,
        servicio_asignado,
        orden_asignada,
        onu_para_retiro,
    )


async def verificar_acceso_cliente(
    db: AsyncSession,
    usuario: UsuarioModel,
    cliente_id: int,
) -> None:
    """Impide que un técnico consulte clientes que no tiene asignados."""
    if usuario.rol != "tecnico":
        return

    cliente_asignado = (
        await db.execute(
            select(ClienteModel.id).where(
                ClienteModel.id == cliente_id,
                filtro_clientes_del_tecnico(usuario.id),
            )
        )
    ).scalar_one_or_none()
    if cliente_asignado is None:
        raise PermissionError(
            "El cliente no está asignado a este técnico"
        )


async def verificar_instalacion_asignada(
    db: AsyncSession,
    usuario: UsuarioModel,
    cliente_id: int,
) -> None:
    """Exige una orden de instalación abierta asignada al técnico."""
    if usuario.rol != "tecnico":
        return

    orden_id = (
        await db.execute(
            select(OrdenServicioModel.id)
            .where(
                OrdenServicioModel.cliente_id == cliente_id,
                OrdenServicioModel.tecnico_id == usuario.id,
                OrdenServicioModel.tipo == "instalacion",
                OrdenServicioModel.estado.in_(ESTADOS_ORDEN_ABIERTA),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if orden_id is None:
        raise PermissionError(
            "No tienes una orden de instalación abierta para este cliente"
        )
