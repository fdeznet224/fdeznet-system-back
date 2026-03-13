from sqlalchemy.ext.asyncio import AsyncSession
from src.infrastructure.models import LogCronjobModel

async def registrar_log(
    db: AsyncSession, 
    origen: str,    # Ej: "Facturación", "Cortes"
    mensaje: str,   # Ej: "Se procesaron 50 clientes"
    nivel: str = "INFO" # "INFO", "ERROR", "WARNING"
):
    """
    Guarda un evento en la base de datos de forma segura.
    No detiene el programa si falla el log.
    """
    try:
        nuevo_log = LogCronjobModel(
            origen=origen,
            mensaje=str(mensaje),
            nivel=nivel
        )
        db.add(nuevo_log)
        await db.commit()
    except Exception as e:
        print(f"⚠️ Error guardando log: {e}")