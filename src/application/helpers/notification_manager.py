from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.infrastructure.models import PlantillaMensajeModel
from src.application.helpers.message_formatter import formatear_mensaje
from src.infrastructure.whatsapp_client import enviar_whatsapp

async def enviar_notificacion_automatica(db: AsyncSession, tipo: str, datos: dict, telefono: str):
    """
    Motor central de notificaciones: Busca la plantilla, la formatea y la envía.
    """
    stmt = select(PlantillaMensajeModel).where(
        PlantillaMensajeModel.tipo == tipo,
        PlantillaMensajeModel.activo == 1
    )
    result = await db.execute(stmt)
    plantilla = result.scalar_one_or_none()

    if plantilla:
        # Usamos el campo 'texto' de tu tabla
        mensaje_final = formatear_mensaje(plantilla.texto, datos)
        return await enviar_whatsapp(telefono, mensaje_final)
    
    print(f"⚠️ No se encontró la plantilla activa para: {tipo}")
    return False