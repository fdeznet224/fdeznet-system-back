"""Aviso por WhatsApp al técnico cuando le asignan una orden o un retiro.

Llega al número de WhatsApp de su usuario. Si falla el envío no se deshace la
asignación: solo queda en el registro. Lleva lo necesario para salir sin abrir
el sistema: a dónde ir (con la ruta en Maps), la caja NAP y qué debe hacer.
"""

import logging
import os
import re
from urllib.parse import quote

from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.models import (
    CajaNapModel,
    ClienteModel,
    MensajeChatModel,
    OrdenServicioModel,
    ServicioModel,
    UsuarioModel,
)
from src.infrastructure.whatsapp_client import whatsapp_queue

logger = logging.getLogger(__name__)

# La ubicación que manda el cliente queda en la dirección como "lat,lng".
RE_ENLACE = re.compile(r"\s*·?\s*https?://\S+")
RE_COORDENADAS = re.compile(r"(-?\d{1,2}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)")

TIPOS = {
    "instalacion": "🛠️ Nueva instalación",
    "soporte": "🔧 Orden de soporte",
    "reparacion": "🔧 Reparación",
    "cambio_domicilio": "🏠 Cambio de domicilio",
    "retiro": "📦 Retiro de equipo",
}
PRIORIDADES = {"alta": "Alta", "urgente": "URGENTE"}
CATEGORIAS = {
    "sin_internet": "Sin internet",
    "lentitud": "Lentitud",
    "potencia_baja": "Potencia alta / señal débil",
    "router_wifi": "Router o WiFi",
    "cable_roto": "Fibra o cable roto",
    "cambio_domicilio": "Cambio de domicilio",
    "otro": "Otro",
}


def ruta_en_maps(latitud, longitud, direccion: str | None = None) -> str | None:
    """Ruta en Google Maps hasta el domicilio, solo por coordenadas.

    Sin GPS se usan las coordenadas que vengan en la dirección (la ubicación
    que mandó el cliente); el texto de la dirección no se usa porque Maps lo
    ubica mal en colonias y rancherías.
    """
    try:
        lat, lng = float(latitud), float(longitud)
    except (TypeError, ValueError):
        lat = lng = 0.0
    if lat == 0 and lng == 0:
        encontradas = RE_COORDENADAS.search(direccion or "")
        if not encontradas:
            return None
        lat, lng = float(encontradas.group(1)), float(encontradas.group(2))
    destino = quote(f"{lat},{lng}")
    return f"https://www.google.com/maps/dir/?api=1&destination={destino}&travelmode=driving"


def texto_asignacion(orden, cliente, base_url: str | None, servicio=None, caja=None) -> str:
    titulo = TIPOS.get(orden.tipo, "📋 Nueva orden")
    nombre = (cliente.nombre if cliente else None) or orden.prospecto_nombre or "Cliente"
    direccion = (
        (servicio.direccion if servicio else None)
        or (cliente.direccion if cliente else None)
        or orden.prospecto_direccion
    )
    contrato = getattr(cliente, "cedula", None)
    lineas = [f"*{titulo} asignada* #{orden.id}", f"👤 {nombre}" + (f" (contrato {contrato})" if contrato else "")]
    categoria = CATEGORIAS.get(getattr(orden, "categoria_soporte", None) or "")
    if categoria:
        lineas.append(f"🔧 Falla: {categoria}")
    # El enlace de ubicación que mandó el cliente no se repite: va en la ruta.
    direccion_texto = RE_ENLACE.sub("", direccion or "").strip()
    if direccion_texto:
        lineas.append(f"📍 {direccion_texto}")
    ubicacion = servicio if servicio and getattr(servicio, "latitud", None) is not None else cliente
    ruta = ruta_en_maps(getattr(ubicacion, "latitud", None), getattr(ubicacion, "longitud", None), direccion)
    if ruta:
        lineas.append(f"🗺️ Cómo llegar: {ruta}")
    if caja:
        puerto = getattr(servicio or cliente, "puerto_nap", None)
        lineas.append(f"📦 Caja {caja.nombre}" + (f", puerto {puerto}" if puerto else ""))
    if orden.fecha_programada:
        lineas.append(f"📅 {orden.fecha_programada:%d/%m %H:%M}")
    if orden.prioridad in PRIORIDADES:
        lineas.append(f"⚠️ Prioridad {PRIORIDADES[orden.prioridad]}")
    if orden.descripcion:
        lineas.append(f"📝 {orden.descripcion[:600]}")
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
        servicio_id = getattr(orden, "servicio_id", None)
        servicio = await db.get(ServicioModel, servicio_id) if servicio_id else None
        caja_id = getattr(servicio, "caja_nap_id", None) or getattr(cliente, "caja_nap_id", None)
        caja = await db.get(CajaNapModel, caja_id) if caja_id else None
        registro = MensajeChatModel(
            cliente_id=None,
            telefono=whatsapp_queue.service._formatear_numero(tecnico.telefono_whatsapp),
            direccion="salida",
            mensaje=texto_asignacion(
                orden, cliente, os.getenv("PUBLIC_URL", "").strip() or None, servicio, caja
            ),
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
