"""
Avisos por WhatsApp al cliente sobre su reporte de falla.

- Al levantar el reporte sin técnico: "registramos tu reporte #N".
  Si lo levantó el agente de WhatsApp no se manda: ya se lo dijo en la plática.
- Al asignarle técnico (al crear o después): "tu reporte #N ya tiene técnico".

Los textos son plantillas editables (Configuración → Plantillas). Un aviso
que falla nunca frena el reporte.
"""

import logging
import re
from typing import Optional

from sqlalchemy import select

from src.infrastructure.models import (
    ClienteModel,
    ConfiguracionSistema,
    MensajeChatModel,
    OrdenServicioModel,
    PlantillaMensajeModel,
    UsuarioModel,
)

logger = logging.getLogger(__name__)

FALLAS = {
    "potencia_baja": "potencia alta / señal débil",
    "cable_roto": "fibra o cable roto",
    "sin_internet": "sin internet",
    "lentitud": "lentitud",
    "router_wifi": "router o WiFi",
    "otro": "falla",
}

MENSAJE_REPORTE_REGISTRADO = (
    "🛠️ Hola {nombre}, registramos tu reporte #{folio} ({falla}). "
    "En breve te asignamos un técnico y te avisaremos cuando vaya en camino. — {empresa}"
)
MENSAJE_REPORTE_TECNICO = (
    "🛠️ Hola {nombre}, tu reporte #{folio} ({falla}) ya tiene técnico asignado: {tecnico}. "
    "Te avisaremos cuando vaya en camino. — {empresa}"
)


def texto_aviso(plantilla: Optional[str], predeterminado: str, variables: dict) -> str:
    texto = plantilla or predeterminado
    for llave, valor in variables.items():
        texto = texto.replace("{" + llave + "}", str(valor))
    return re.sub(r" {2,}", " ", texto).strip()


async def avisar_cliente_reporte(db, orden: OrdenServicioModel, usuario_id: Optional[int]) -> bool:
    """Al levantar el reporte: con técnico avisa la asignación; sin técnico, que quedó registrado."""
    if orden.tecnico_id:
        return await avisar_cliente_tecnico(db, orden, usuario_id)
    if orden.canal_reporte == "whatsapp":
        return False
    return await _encolar(db, orden, "reporte_registrado", MENSAJE_REPORTE_REGISTRADO, f"orden:{orden.id}:reporte_registrado", usuario_id)


async def avisar_cliente_tecnico(db, orden: OrdenServicioModel, usuario_id: Optional[int]) -> bool:
    if not orden.tecnico_id or not orden.categoria_soporte:
        return False
    return await _encolar(
        db, orden, "reporte_tecnico_asignado", MENSAJE_REPORTE_TECNICO,
        f"orden:{orden.id}:tecnico:{orden.tecnico_id}", usuario_id,
    )


async def _encolar(db, orden, tipo: str, predeterminado: str, clave: str, usuario_id: Optional[int]) -> bool:
    try:
        cliente = await db.get(ClienteModel, orden.cliente_id) if orden.cliente_id else None
        if not cliente or not cliente.telefono:
            return False
        if (await db.execute(select(MensajeChatModel.id).where(MensajeChatModel.clave_dedupe == clave))).scalar_one_or_none():
            return False
        from src.infrastructure.whatsapp_client import whatsapp_queue

        plantilla = (
            await db.execute(
                select(PlantillaMensajeModel).where(
                    PlantillaMensajeModel.tipo == tipo, PlantillaMensajeModel.activo.is_(True)
                )
            )
        ).scalar_one_or_none()
        tecnico = await db.get(UsuarioModel, orden.tecnico_id) if orden.tecnico_id else None
        marca = await db.get(ConfiguracionSistema, 1)
        nombre = (cliente.nombre or "").split()
        texto = texto_aviso(
            plantilla.texto if plantilla else None,
            predeterminado,
            {
                "nombre": nombre[0] if nombre else "",
                "folio": orden.id,
                "falla": FALLAS.get(orden.categoria_soporte or "", "falla"),
                "tecnico": ((tecnico.nombre_completo or tecnico.usuario) if tecnico else "").split(" ")[0],
                "contrato": cliente.cedula or "",
                "empresa": getattr(marca, "empresa_nombre", None) or "tu proveedor de internet",
            },
        )
        registro = MensajeChatModel(
            cliente_id=cliente.id,
            telefono=whatsapp_queue.service._formatear_numero(cliente.telefono),
            direccion="salida",
            mensaje=texto,
            tipo_mensaje="texto",
            tipo_evento=tipo,
            clave_dedupe=clave,
            leido=True,
            ack=0,
            estado_envio="pendiente",
            creado_por_id=usuario_id,
        )
        db.add(registro)
        await db.commit()
        await whatsapp_queue.agregar_tarea({"mensaje_chat_id": registro.id})
        return True
    except Exception:
        logger.exception("No se pudo avisar al cliente del reporte %s", orden.id)
        await db.rollback()
        return False
