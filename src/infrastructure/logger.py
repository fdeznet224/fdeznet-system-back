from sqlalchemy.ext.asyncio import AsyncSession
from src.infrastructure.models import LogActividadModel

async def registrar_log(db: AsyncSession, usuario_id: int, accion: str, detalle: str, ip: str = "0.0.0.0"):
    """
    Guarda un evento en la base de datos para auditoría.
    """
    nuevo_log = LogActividadModel(
        usuario_id=usuario_id,
        accion=accion,
        detalle=detalle,
        ip_cliente=ip
    )
    db.add(nuevo_log)
    # Nota: No hacemos commit aquí porque usualmente esto va dentro de otra transacción mayor.
    # Si se usa aislado, recordar hacer commit fuera.