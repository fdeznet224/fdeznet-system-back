# En src/infrastructure/utils.py
from src.infrastructure.models import LogActividadModel

async def registrar_log(db, usuario, accion, detalle, ip="0.0.0.0"):
    nuevo_log = LogActividadModel(
        usuario_id=usuario.id,
        usuario_nombre=usuario.usuario,
        accion=accion,
        metodo="SYSTEM",
        ruta="internal",
        estado_http=200,
        detalle=detalle,
        ip_cliente=ip
    )
    db.add(nuevo_log)
    # El commit se hace en la función principal (caja)
