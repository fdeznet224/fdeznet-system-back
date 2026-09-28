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


def clave_telefono(valor: str | None) -> str | None:
    """Últimos 10 dígitos: igualan 52…, 521…, @c.us y el número local."""
    base = str(valor or "").split("@")[0]
    digitos = re.sub(r"\D", "", base)
    if len(digitos) < 10 or str(valor or "").lower().endswith("@lid"):
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
    else:
        db.add(BotPausaModel(telefono=clave, pausado_hasta=hasta, motivo=motivo))
    return hasta


async def bot_en_pausa(db: AsyncSession, telefono: str | None) -> bool:
    clave = clave_telefono(telefono)
    if not clave:
        return False
    pausa = await db.get(BotPausaModel, clave)
    return bool(pausa and pausa.pausado_hasta > datetime.now())
