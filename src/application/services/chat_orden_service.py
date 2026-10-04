"""Conversación de WhatsApp de una orden de instalación.

Si la orden ya tiene cliente es la conversación de ese cliente. Si es de un
prospecto, sus mensajes no tienen cliente: se buscan por el chat que anotó el
agente ("Chat: 8276...@lid") y por el teléfono que dejó.
"""

import re
from typing import Iterable, Optional

from sqlalchemy import func, or_, select

from src.infrastructure.models import MensajeChatModel, OrdenServicioModel

RE_CHAT = re.compile(r"Chat:\s*(\S+)")


def telefonos_de_orden(orden: OrdenServicioModel) -> list[str]:
    """Identificadores del chat del prospecto, el preferido para responder primero."""
    telefonos = []
    chat = RE_CHAT.search(orden.descripcion or "")
    if chat:
        telefonos.append(chat.group(1).rstrip(".,;"))
    digitos = re.sub(r"\D", "", orden.prospecto_telefono or "")[-10:]
    if len(digitos) == 10:
        telefonos += [f"521{digitos}", f"52{digitos}", digitos]
    return list(dict.fromkeys(telefonos))


def filtro_mensajes(orden: OrdenServicioModel):
    if orden.cliente_id:
        return MensajeChatModel.cliente_id == orden.cliente_id
    telefonos = telefonos_de_orden(orden)
    if not telefonos:
        return None
    return MensajeChatModel.telefono.in_(telefonos)


async def mensajes_de_orden(db, orden: OrdenServicioModel, marcar_leidos: bool = True):
    filtro = filtro_mensajes(orden)
    if filtro is None:
        return []
    mensajes = (
        await db.execute(select(MensajeChatModel).where(filtro).order_by(MensajeChatModel.fecha.asc(), MensajeChatModel.id.asc()))
    ).scalars().all()
    no_leidos = [m for m in mensajes if m.direccion == "entrada" and not m.leido]
    if marcar_leidos and no_leidos:
        for mensaje in no_leidos:
            mensaje.leido = True
        await db.commit()
    return mensajes


async def no_leidos_por_orden(db, ordenes: Iterable[OrdenServicioModel]) -> dict[str, dict]:
    """{orden_id: {count, antiguedad}} de las órdenes con mensajes sin leer."""
    ordenes = list(ordenes)
    clientes = {o.cliente_id for o in ordenes if o.cliente_id}
    telefonos = {t for o in ordenes if not o.cliente_id for t in telefonos_de_orden(o)}
    if not clientes and not telefonos:
        return {}
    condiciones = []
    if clientes:
        condiciones.append(MensajeChatModel.cliente_id.in_(clientes))
    if telefonos:
        condiciones.append(MensajeChatModel.telefono.in_(telefonos))
    filas = (
        await db.execute(
            select(
                MensajeChatModel.cliente_id,
                MensajeChatModel.telefono,
                func.count(MensajeChatModel.id),
                func.min(MensajeChatModel.fecha),
            )
            .where(MensajeChatModel.direccion == "entrada", MensajeChatModel.leido.is_(False), or_(*condiciones))
            .group_by(MensajeChatModel.cliente_id, MensajeChatModel.telefono)
        )
    ).all()
    resultado = {}
    for orden in ordenes:
        propios = [
            f for f in filas
            if (f[0] == orden.cliente_id if orden.cliente_id else f[1] in telefonos_de_orden(orden))
        ]
        total = sum(f[2] for f in propios)
        if total:
            fechas = [f[3] for f in propios if f[3]]
            resultado[str(orden.id)] = {
                "count": total,
                "antiguedad": min(fechas).isoformat() if fechas else None,
            }
    return resultado


def telefono_para_responder(orden: OrdenServicioModel) -> Optional[str]:
    telefonos = telefonos_de_orden(orden)
    return telefonos[0] if telefonos else None
