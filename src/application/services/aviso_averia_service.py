"""Aviso por WhatsApp a los clientes afectados por una avería.

Se elige la caja NAP, el puerto PON de una OLT o la zona que falla; se manda
el mismo mensaje a cada cliente afectado por la cola de salida (respeta el
intervalo entre mensajes y queda en la bandeja de WhatsApp con su lote).
"""

from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.models import MensajeChatModel
from src.infrastructure.whatsapp_client import GLOBAL_SETTINGS, whatsapp_queue

MENSAJE_SUGERIDO = (
    "Hola {nombre}, te avisamos que hay una falla en la red de tu zona y ya estamos "
    "trabajando para resolverla. Tu servicio puede estar intermitente o sin conexión "
    "mientras tanto. Te avisaremos cuando quede restablecido. Gracias por tu paciencia."
)


def personalizar(mensaje: str, nombre: str | None) -> str:
    primer_nombre = (nombre or "").split()[0] if (nombre or "").split() else ""
    return mensaje.replace("{nombre}", primer_nombre).replace("  ", " ").replace(" ,", ",")


async def encolar_aviso(
    db: AsyncSession, afectados: list[dict], mensaje: str, usuario_id: int | None
) -> dict:
    intervalo = GLOBAL_SETTINGS["intervalo_default"]
    lote_id = str(uuid4())
    registros = []
    for afectado in afectados:
        registro = MensajeChatModel(
            cliente_id=afectado["cliente_id"],
            telefono=whatsapp_queue.service._formatear_numero(afectado["telefono"]),
            direccion="salida",
            mensaje=personalizar(mensaje, afectado.get("nombre")),
            tipo_mensaje="texto",
            tipo_evento="aviso_averia",
            leido=True,
            ack=0,
            estado_envio="pendiente",
            lote_id=lote_id,
            creado_por_id=usuario_id,
            intervalo_salida=intervalo,
        )
        db.add(registro)
        registros.append(registro)
    await db.commit()
    for registro in registros:
        await whatsapp_queue.agregar_tarea({"mensaje_chat_id": registro.id, "intervalo": intervalo})
    return {"lote_id": lote_id, "total_mensajes": len(registros), "intervalo_segundos": intervalo}
