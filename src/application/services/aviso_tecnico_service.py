"""Aviso por WhatsApp al técnico cuando le asignan una orden o un retiro.

Llega al número de WhatsApp de su usuario. Si falla el envío no se deshace la
asignación: solo queda en el registro.
"""

import logging
import os

from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.models import (
    ClienteModel,
    MensajeChatModel,
    OrdenServicioModel,
    UsuarioModel,
)
from src.infrastructure.whatsapp_client import whatsapp_queue

logger = logging.getLogger(__name__)

TIPOS = {
    "instalacion": "🛠️ Nueva instalación",
    "soporte": "🔧 Orden de soporte",
    "retiro": "📦 Retiro de equipo",
}
PRIORIDADES = {"alta": "Alta", "urgente": "URGENTE"}


def texto_asignacion(orden, cliente, base_url: str | None) -> str:
    titulo = TIPOS.get(orden.tipo, "📋 Nueva orden")
    nombre = (cliente.nombre if cliente else None) or orden.prospecto_nombre or "Cliente"
    direccion = (cliente.direccion if cliente else None) or orden.prospecto_direccion
    lineas = [f"*{titulo} asignada* #{orden.id}", f"👤 {nombre}"]
    if direccion:
        lineas.append(f"📍 {direccion}")
    if orden.fecha_programada:
        lineas.append(f"📅 {orden.fecha_programada:%d/%m %H:%M}")
    if orden.prioridad in PRIORIDADES:
        lineas.append(f"⚠️ Prioridad {PRIORIDADES[orden.prioridad]}")
    if orden.descripcion:
        lineas.append(f"📝 {orden.descripcion[:200]}")
    if base_url:
        lineas.append(f"\nÁbrela en tu agenda: {base_url.rstrip('/')}/tech/dashboard")
    return "\n".join(lineas)


async def avisar_asignacion(db: AsyncSession, orden_id: int, asignado_por_id: int | None) -> bool:
    """Manda el aviso si la orden tiene técnico y no se la asignó él mismo."""
    try:
        orden = await db.get(OrdenServicioModel, orden_id)
        if not orden or not orden.tecnico_id or orden.tecnico_id == asignado_por_id:
            return False
        tecnico = await db.get(UsuarioModel, orden.tecnico_id)
        if not tecnico or not tecnico.activo or not tecnico.telefono_whatsapp:
            return False
        cliente = await db.get(ClienteModel, orden.cliente_id) if orden.cliente_id else None
        registro = MensajeChatModel(
            cliente_id=None,
            telefono=whatsapp_queue.service._formatear_numero(tecnico.telefono_whatsapp),
            direccion="salida",
            mensaje=texto_asignacion(orden, cliente, os.getenv("PUBLIC_URL", "").strip() or None),
            tipo_mensaje="texto",
            tipo_evento="aviso_tecnico",
            leido=True,
            ack=0,
            estado_envio="pendiente",
            creado_por_id=asignado_por_id,
        )
        db.add(registro)
        await db.commit()
        await whatsapp_queue.agregar_tarea({"mensaje_chat_id": registro.id})
        return True
    except Exception:
        logger.exception("No se pudo avisar al técnico de la orden %s", orden_id)
        await db.rollback()
        return False
