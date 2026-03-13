from typing import List
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete, desc
from sqlalchemy.exc import IntegrityError 

# --- INFRAESTRUCTURA (Tus Modelos y DB) ---
from src.infrastructure.database import get_db
from src.infrastructure.models import (
    PlantillaFacturacionModel, 
    PlantillaMensajeModel, 
    ConfiguracionModel,
    ConfiguracionSistema,
    LogCronjobModel  # 👈 Importante: El modelo de Logs
)

# --- SCHEMAS (Los datos que entran y salen) ---
from src.domain.schemas import (
    SystemConfigUpdate, 
    ConfigUpdate, 
    BillingTemplateRequest, 
    MessageTemplateRequest,
    PlantillaMensajeResponse,
    PlantillaResponse,
    LogCronjobResponse # 👈 Importante: El schema de respuesta para Logs
)

# ✅ El prefijo es '/configuracion', así que la ruta final será '/configuracion/logs'
router = APIRouter(prefix="/configuracion", tags=["Configuración General"])


# =========================================================
# 1. PLANTILLAS DE FACTURACIÓN (Ciclos de Cobro)
# =========================================================

@router.get("/plantillas-facturacion", response_model=List[PlantillaResponse])
async def listar_plantillas_facturacion(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(PlantillaFacturacionModel))
    return result.scalars().all()

@router.post("/plantillas-facturacion")
async def crear_plantilla_facturacion(data: BillingTemplateRequest, db: AsyncSession = Depends(get_db)):
    nueva = PlantillaFacturacionModel(**data.dict())
    db.add(nueva)
    await db.commit()
    await db.refresh(nueva)
    return nueva

@router.put("/plantillas-facturacion/{id}")
async def actualizar_plantilla_facturacion(id: int, data: BillingTemplateRequest, db: AsyncSession = Depends(get_db)):
    plantilla = await db.get(PlantillaFacturacionModel, id)
    if not plantilla:
        raise HTTPException(status_code=404, detail="Plantilla no encontrada")
    
    for key, value in data.dict().items():
        setattr(plantilla, key, value)
    
    await db.commit()
    await db.refresh(plantilla)
    return plantilla

@router.delete("/plantillas-facturacion/{id}")
async def eliminar_plantilla_facturacion(id: int, db: AsyncSession = Depends(get_db)):
    plantilla = await db.get(PlantillaFacturacionModel, id)
    if not plantilla:
        raise HTTPException(status_code=404, detail="Plantilla no encontrada")
    
    try:
        await db.delete(plantilla)
        await db.commit()
        return {"status": "success", "mensaje": "Ciclo eliminado correctamente"}
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=400, detail="No se puede eliminar: Hay clientes asignados a este ciclo.")
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# =========================================================
# 2. PLANTILLAS DE MENSAJES (WhatsApp)
# =========================================================

@router.get("/plantillas", response_model=List[PlantillaMensajeResponse])
async def listar_plantillas_mensajes(db: AsyncSession = Depends(get_db)):
    """Lista todas las plantillas de mensajes disponibles."""
    result = await db.execute(select(PlantillaMensajeModel))
    return result.scalars().all()

@router.post("/plantillas")
async def crear_plantilla_mensaje(data: MessageTemplateRequest, db: AsyncSession = Depends(get_db)):
    """Crea una nueva plantilla (Valida que el tipo no se repita)."""
    # Verificar si ya existe ese tipo (bienvenida, aviso_corte, etc)
    query = select(PlantillaMensajeModel).where(PlantillaMensajeModel.tipo == data.tipo)
    result = await db.execute(query)
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail=f"Ya existe una plantilla tipo '{data.tipo}'")

    nueva = PlantillaMensajeModel(**data.dict())
    db.add(nueva)
    await db.commit()
    await db.refresh(nueva)
    return nueva

@router.put("/plantillas/{id}")
async def actualizar_plantilla_mensaje(id: int, data: MessageTemplateRequest, db: AsyncSession = Depends(get_db)):
    """Actualiza una plantilla existente por su ID."""
    plantilla = await db.get(PlantillaMensajeModel, id)
    if not plantilla:
        raise HTTPException(status_code=404, detail="Plantilla no encontrada")
    
    # Actualizamos los campos
    plantilla.tipo = data.tipo
    plantilla.texto = data.texto
    plantilla.activo = data.activo
    
    await db.commit()
    await db.refresh(plantilla)
    return plantilla

@router.delete("/plantillas/{id}")
async def eliminar_plantilla_mensaje(id: int, db: AsyncSession = Depends(get_db)):
    """Elimina una plantilla de mensaje."""
    plantilla = await db.get(PlantillaMensajeModel, id)
    if not plantilla:
        raise HTTPException(status_code=404, detail="Plantilla no encontrada")
    
    await db.delete(plantilla)
    await db.commit()
    return {"status": "success", "mensaje": "Plantilla eliminada correctamente"}


# =========================================================
# 3. CONFIGURACIÓN DEL SISTEMA (Cronjobs Globales)
# =========================================================

@router.get("/sistema")
async def obtener_configuracion_sistema(db: AsyncSession = Depends(get_db)):
    stmt = select(ConfiguracionSistema).where(ConfiguracionSistema.id == 1)
    result = await db.execute(stmt)
    config = result.scalar_one_or_none()
    
    if not config:
        config = ConfiguracionSistema(id=1)
        db.add(config)
        await db.commit()
        await db.refresh(config)
    
    return config

@router.put("/sistema")
async def guardar_configuracion_sistema(datos: SystemConfigUpdate, db: AsyncSession = Depends(get_db)):
    stmt = update(ConfiguracionSistema).where(ConfiguracionSistema.id == 1).values(**datos.dict())
    await db.execute(stmt)
    await db.commit()
    return {"status": "ok", "mensaje": "Configuración guardada"}


# =========================================================
# 4. PPPOE DEFAULT (Legacy)
# =========================================================

@router.get("/pppoe-default")
async def obtener_password_default(db: AsyncSession = Depends(get_db)):
    stmt = select(ConfiguracionModel).where(ConfiguracionModel.clave == 'pppoe_password_default')
    res = await db.execute(stmt)
    config = res.scalar()
    return {"password": config.valor if config else "12345"}

@router.post("/pppoe-default")
async def cambiar_password_default(data: ConfigUpdate, db: AsyncSession = Depends(get_db)):
    stmt = select(ConfiguracionModel).where(ConfiguracionModel.clave == 'pppoe_password_default')
    res = await db.execute(stmt)
    config_db = res.scalar()
    
    if not config_db:
        db.add(ConfiguracionModel(clave='pppoe_password_default', valor=data.valor))
    else:
        config_db.valor = data.valor
        
    await db.commit()
    return {"status": "ok"}


# =========================================================
# 5. HISTORIAL DE LOGS (✅ NUEVA CONEXIÓN)
# =========================================================
# Estos endpoints conectan con la pantalla negra "CronjobLogs.tsx"

@router.get("/logs", response_model=List[LogCronjobResponse])
async def obtener_historial_logs(limit: int = 100, db: AsyncSession = Depends(get_db)):
    """Devuelve los últimos 100 eventos ordenados por fecha descendente."""
    stmt = select(LogCronjobModel).order_by(desc(LogCronjobModel.fecha)).limit(limit)
    result = await db.execute(stmt)
    return result.scalars().all()

@router.delete("/logs")
async def limpiar_historial_logs(db: AsyncSession = Depends(get_db)):
    """Elimina todos los registros de la tabla logs."""
    await db.execute(delete(LogCronjobModel))
    await db.commit()
    return {"status": "success", "mensaje": "Historial depurado correctamente"}