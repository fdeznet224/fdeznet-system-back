"""Servicio de los clientes que llegan por el Excel de importación.

Son clientes que ya están instalados y navegando en el MikroTik: su servicio
nace activo, sin orden de instalación, sin tocar el router y sin prorrateo. La
primera mensualidad completa sale el próximo día de pago de su plantilla.
"""
import calendar
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.services.billing_calendar_service import BillingCalendarService
from src.infrastructure.models import (
    CicloFacturacion,
    ClienteModel,
    PlantillaFacturacionModel,
    ServicioModel,
    TipoFacturacion,
)


def proximo_dia_de_pago(hoy: date, dia_pago: int | None) -> date:
    """Hoy si hoy es el día de pago; si no, el siguiente (31 → último día del mes)."""
    if not dia_pago:
        return hoy
    dia = min(dia_pago, calendar.monthrange(hoy.year, hoy.month)[1])
    if dia >= hoy.day:
        return hoy.replace(day=dia)
    siguiente = BillingCalendarService.add_months(hoy.replace(day=1), 1)
    return siguiente.replace(day=min(dia_pago, calendar.monthrange(siguiente.year, siguiente.month)[1]))


async def crear_servicio_importado(
    db: AsyncSession, cliente: ClienteModel, hoy: date | None = None
) -> ServicioModel:
    hoy = hoy or date.today()
    plantilla = (
        await db.get(PlantillaFacturacionModel, cliente.plantilla_id)
        if cliente.plantilla_id
        else None
    )
    ciclo = BillingCalendarService.resolver_ciclo(None, plantilla)
    dia_pago = plantilla.dia_pago if plantilla and ciclo == "calendario" else None
    primer_cobro = proximo_dia_de_pago(hoy, dia_pago)
    servicio = ServicioModel(
        cliente_id=cliente.id,
        alias="Principal",
        direccion=cliente.direccion,
        latitud=cliente.latitud,
        longitud=cliente.longitud,
        router_id=cliente.router_id,
        plan_id=cliente.plan_id,
        plantilla_id=cliente.plantilla_id,
        zona_id=cliente.zona_id,
        red_id=cliente.red_id,
        olt_id=cliente.olt_id,
        caja_nap_id=cliente.caja_nap_id,
        puerto_nap=cliente.puerto_nap,
        onu_id=cliente.onu_id,
        ip_asignada=cliente.ip_asignada,
        mac_address=cliente.mac_address,
        user_pppoe=cliente.user_pppoe,
        pass_pppoe=cliente.pass_pppoe,
        is_online=False,
        ultimo_cambio_estado=None,
        tipo_facturacion=TipoFacturacion.prepago,
        ciclo_facturacion=CicloFacturacion(ciclo),
        meses_gratis=0,
        fecha_instalacion=hoy,
        fecha_activacion=hoy,
        fecha_inicio_servicio=hoy,
        fecha_inicio_cobro=primer_cobro,
        proxima_facturacion=primer_cobro,
        dia_vencimiento=dia_pago if dia_pago else hoy.day,
        dias_tolerancia=(plantilla.dias_tolerancia or 0) if plantilla else 0,
        estado="activo",
    )
    db.add(servicio)
    await db.flush()
    return servicio


async def reparar_importados_sin_servicio(db: AsyncSession) -> int:
    """Crea el servicio de los clientes activos importados que quedaron sin él."""
    con_servicio = select(ServicioModel.cliente_id)
    clientes = (
        await db.execute(
            select(ClienteModel).where(
                ClienteModel.estado == "activo",
                ClienteModel.id.not_in(con_servicio),
            )
        )
    ).scalars().all()
    for cliente in clientes:
        await crear_servicio_importado(db, cliente)
    await db.commit()
    return len(clientes)
