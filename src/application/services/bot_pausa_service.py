"""Pausa del bot por chat mientras un asesor atiende al cliente.

Cuando alguien del equipo responde (desde el panel o desde el celular) o el
cliente pide hablar con un asesor, el bot deja de contestar solo en ese chat
durante `PAUSA_ASESOR`. Un comando explícito del cliente sigue funcionando.
"""

import re
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.models import BotPausaModel

PAUSA_ASESOR = timedelta(hours=2)
# Cuando alguien del equipo escribe por un detalle, el agente vuelve a
# atender el chat 15 minutos después de su último mensaje.
PAUSA_INTERVENCION = timedelta(minutes=15)
# Pausas que al vencer se retoman solas si el cliente quedó sin respuesta.
MOTIVOS_RETOMABLES = {"respuesta_celular", "respuesta_panel", "agente_pasa_a_asesor"}
# No se retoman pausas que vencieron hace más de esto (por ejemplo tras un reinicio largo).
VENTANA_RETOMAR = timedelta(minutes=30)


def clave_telefono(valor: str | None) -> str | None:
    """Últimos 10 dígitos: igualan 52…, 521…, @c.us y el número local.

    Un LID de WhatsApp no es un teléfono: se usa completo para que la pausa
    del asesor también funcione en esos chats.
    """
    base = str(valor or "").split("@")[0]
    digitos = re.sub(r"\D", "", base)
    if str(valor or "").lower().endswith("@lid"):
        return digitos[:20] or None
    if len(digitos) < 10:
        return None
    return digitos[-10:]


async def pausar_bot(
    db: AsyncSession,
    telefono: str | None,
    motivo: str,
    duracion: timedelta = PAUSA_ASESOR,
) -> datetime | None:
    clave = clave_telefono(telefono)
    if not clave:
        return None
    hasta = datetime.now() + duracion
    pausa = await db.get(BotPausaModel, clave)
    if pausa:
        pausa.pausado_hasta = max(pausa.pausado_hasta, hasta)
        pausa.motivo = motivo
        # Alguien del equipo atendió el chat: lo anterior ya no queda pendiente.
        pausa.mensaje_pendiente_id = None
    else:
        db.add(BotPausaModel(telefono=clave, pausado_hasta=hasta, motivo=motivo))
    return hasta


async def anotar_mensaje_en_pausa(db: AsyncSession, telefono: str | None, mensaje_chat_id: int) -> bool:
    """Si el chat está pausado, guarda este mensaje para atenderlo al reanudar."""
    clave = clave_telefono(telefono)
    if not clave:
        return False
    pausa = await db.get(BotPausaModel, clave)
    if not pausa or pausa.pausado_hasta <= datetime.now():
        return False
    pausa.mensaje_pendiente_id = mensaje_chat_id
    return True


async def bot_en_pausa(db: AsyncSession, telefono: str | None) -> bool:
    clave = clave_telefono(telefono)
    if not clave:
        return False
    pausa = await db.get(BotPausaModel, clave)
    return bool(pausa and pausa.pausado_hasta > datetime.now())


def _entrada_para_agente(mensaje: str | None) -> tuple[str, str | None] | None:
    """(texto, imagen) del mensaje guardado, como lo recibió el webhook."""
    texto = str(mensaje or "").strip()
    if "[AUDIO]" in texto.upper():
        return None  # los audios los escucha un asesor
    for prefijo in ("📷 [Imagen enviada] ", "📎 [Archivo adjunto] "):
        if texto.startswith(prefijo):
            return "", texto[len(prefijo):].strip() or None
    return texto, None


async def retomar_chats_pausados(db: AsyncSession, lanzar, ahora: datetime | None = None) -> int:
    """Al vencer la pausa por un asesor, el agente atiende lo que quedó sin respuesta.

    El mensaje pendiente se anota al llegar (con la misma llave de la pausa,
    que ya resuelve el @lid al número) y se borra si el asesor vuelve a
    escribir. `lanzar(telefono_raw, telefono_busqueda, mensaje_chat_id, texto,
    media_url)` es el mismo arranque del agente que usa el webhook.
    """
    from sqlalchemy import select

    from src.infrastructure.models import MensajeChatModel

    ahora = ahora or datetime.now()
    pausas = (
        await db.execute(
            select(BotPausaModel).where(
                BotPausaModel.motivo.in_(MOTIVOS_RETOMABLES),
                BotPausaModel.mensaje_pendiente_id.isnot(None),
                BotPausaModel.pausado_hasta <= ahora,
                BotPausaModel.pausado_hasta >= ahora - VENTANA_RETOMAR,
            )
        )
    ).scalars().all()
    retomados = 0
    for pausa in pausas:
        mensaje = await db.get(MensajeChatModel, pausa.mensaje_pendiente_id)
        pausa.mensaje_pendiente_id = None  # una sola vez
        if not mensaje or mensaje.direccion != "entrada":
            continue
        entrada = _entrada_para_agente(mensaje.mensaje)
        if entrada is None:
            continue
        texto, imagen = entrada
        lanzar(mensaje.telefono, mensaje.telefono, mensaje.id, texto, imagen)
        retomados += 1
    await db.commit()
    return retomados
