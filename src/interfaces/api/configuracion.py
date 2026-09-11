import asyncio
import hashlib
import io
import json
from pathlib import Path
from typing import List
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from PIL import Image, UnidentifiedImageError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete, desc
from sqlalchemy.exc import IntegrityError 
from fastapi_cache import FastAPICache
from fastapi_cache.decorator import cache

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
    LogCronjobResponse, # 👈 Importante: El schema de respuesta para Logs
    BrandingConfig,
    LocalLicenseStatus,
    MaintenanceStatus,
    StoragePolicyUpdate,
)
from src.application.services.license_service import local_status, verify_license
from src.application.services.branding_service import get_or_create_system_config
from src.infrastructure.auth import get_current_active_user
from src.application.services.storage_service import (
    cleanup_storage,
    close_period,
    storage_dashboard,
    update_backup_retention,
)

# ✅ El prefijo es '/configuracion', así que la ruta final será '/configuracion/logs'
router = APIRouter(prefix="/configuracion", tags=["Configuración General"])
public_router = APIRouter(prefix="/public", tags=["Configuración Pública"])
license_router = APIRouter(prefix="/licencia", tags=["Licencia"])
MAINTENANCE_STATUS_FILE = Path("/var/lib/fdeznet/maintenance-status.json")
MANUAL_UPDATE_REQUEST_FILE = Path("/var/lib/fdeznet/manual-update-requested")
BRANDING_DIR = Path(__file__).resolve().parents[3] / "static" / "branding"
MAX_BRAND_IMAGE_BYTES = 2 * 1024 * 1024


async def _iniciar_mantenimiento(service: str) -> dict[str, str]:
    process = await asyncio.create_subprocess_exec(
        "sudo",
        "/usr/bin/systemctl",
        "start",
        "--no-block",
        service,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise HTTPException(
            status_code=503,
            detail=detail or "El servicio de mantenimiento no está disponible",
        )
    return {"status": "ok", "mensaje": "La tarea inició en segundo plano"}


async def _obtener_configuracion(db: AsyncSession) -> ConfiguracionSistema:
    return await get_or_create_system_config(db)


@public_router.get("/marca", response_model=BrandingConfig)
async def obtener_marca_publica(db: AsyncSession = Depends(get_db)):
    return await _obtener_configuracion(db)


@public_router.get("/manifest.webmanifest", response_class=JSONResponse)
async def obtener_manifest_pwa(db: AsyncSession = Depends(get_db)):
    marca = await _obtener_configuracion(db)
    if marca.favicon_url and marca.favicon_url.startswith(
        "/api/public/marca/archivo/favicon"
    ):
        icono_512 = marca.favicon_url
        icono_192 = marca.favicon_url.replace(
            "/archivo/favicon", "/archivo/favicon-192", 1
        )
        iconos = [
            {"src": icono_192, "sizes": "192x192", "type": "image/png"},
            {
                "src": icono_512,
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any maskable",
            },
        ]
    else:
        iconos = [
            {"src": "/pwa-192x192.png", "sizes": "192x192", "type": "image/png"},
            {
                "src": "/pwa-512x512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any maskable",
            },
        ]
    return {
        "name": marca.sistema_nombre,
        "short_name": marca.empresa_nombre[:30],
        "description": f"Gestión integral para {marca.empresa_nombre}",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "theme_color": marca.color_primario,
        "background_color": marca.color_secundario,
        "icons": iconos,
    }


@public_router.get("/marca/archivo/{tipo}", response_class=FileResponse)
async def obtener_archivo_marca(tipo: str):
    if tipo not in {"logo", "favicon", "favicon-192"}:
        raise HTTPException(status_code=404, detail="Archivo de marca no encontrado")
    path = BRANDING_DIR / f"{tipo}.png"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Archivo de marca no encontrado")
    return FileResponse(
        path,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@router.put("/marca", response_model=BrandingConfig)
async def guardar_marca(datos: BrandingConfig, db: AsyncSession = Depends(get_db)):
    config = await _obtener_configuracion(db)
    for campo, valor in datos.model_dump().items():
        setattr(config, campo, valor.strip() if isinstance(valor, str) else valor)
    await db.commit()
    await db.refresh(config)
    await FastAPICache.clear()
    return config


@router.post("/marca/{tipo}/archivo", response_model=BrandingConfig)
async def subir_archivo_marca(
    tipo: str,
    archivo: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    if tipo not in {"logo", "favicon"}:
        raise HTTPException(status_code=404, detail="Tipo de imagen no admitido")
    contenido = await archivo.read(MAX_BRAND_IMAGE_BYTES + 1)
    if not contenido or len(contenido) > MAX_BRAND_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="La imagen debe pesar máximo 2 MB")
    try:
        imagen = Image.open(io.BytesIO(contenido))
        imagen.verify()
        imagen = Image.open(io.BytesIO(contenido))
        if imagen.width * imagen.height > 16_000_000:
            raise HTTPException(status_code=400, detail="La resolución de la imagen es demasiado grande")
        imagen = imagen.convert("RGBA")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise HTTPException(status_code=400, detail="El archivo no es una imagen válida") from exc

    BRANDING_DIR.mkdir(parents=True, exist_ok=True)
    destino = BRANDING_DIR / f"{tipo}.png"
    temporal = BRANDING_DIR / f".{tipo}.tmp.png"
    if tipo == "favicon":
        imagen.thumbnail((512, 512), Image.Resampling.LANCZOS)
        favicon = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
        favicon.paste(
            imagen,
            ((512 - imagen.width) // 2, (512 - imagen.height) // 2),
            imagen,
        )
        favicon.save(temporal, format="PNG", optimize=True)
        favicon_192 = favicon.resize((192, 192), Image.Resampling.LANCZOS)
        temporal_192 = BRANDING_DIR / ".favicon-192.tmp.png"
        favicon_192.save(temporal_192, format="PNG", optimize=True)
        temporal_192.replace(BRANDING_DIR / "favicon-192.png")
    else:
        imagen.thumbnail((1600, 800), Image.Resampling.LANCZOS)
        imagen.save(temporal, format="PNG", optimize=True)
    temporal.replace(destino)

    huella = hashlib.sha256(contenido).hexdigest()[:12]
    config = await _obtener_configuracion(db)
    url = f"/api/public/marca/archivo/{tipo}?v={huella}"
    setattr(config, "logo_url" if tipo == "logo" else "favicon_url", url)
    await db.commit()
    await db.refresh(config)
    await FastAPICache.clear()
    return config


@router.get("/licencia", response_model=LocalLicenseStatus)
async def obtener_estado_licencia(db: AsyncSession = Depends(get_db)):
    return local_status(await _obtener_configuracion(db))


@license_router.get("/estado", response_model=LocalLicenseStatus)
async def obtener_estado_licencia_usuario(db: AsyncSession = Depends(get_db)):
    return local_status(await _obtener_configuracion(db))


@router.post("/licencia/verificar", response_model=LocalLicenseStatus)
async def verificar_estado_licencia(db: AsyncSession = Depends(get_db)):
    return await verify_license(db)


@router.get("/mantenimiento", response_model=MaintenanceStatus)
async def obtener_estado_mantenimiento():
    data: dict = {}
    try:
        data = json.loads(MAINTENANCE_STATUS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        pass
    data["actualizacion_automatica"] = Path(
        "/etc/systemd/system/timers.target.wants/fdeznet-update.timer"
    ).exists()
    data["respaldo_automatico"] = Path(
        "/etc/systemd/system/timers.target.wants/fdeznet-backup.timer"
    ).exists()
    data["revision_automatica"] = Path(
        "/etc/systemd/system/timers.target.wants/fdeznet-verify.timer"
    ).exists()
    return MaintenanceStatus.model_validate(data)


@router.post("/mantenimiento/respaldo", status_code=202)
async def iniciar_respaldo():
    return await _iniciar_mantenimiento("fdeznet-backup.service")


@router.post("/mantenimiento/actualizar", status_code=202)
async def iniciar_actualizacion():
    try:
        MANUAL_UPDATE_REQUEST_FILE.write_text("requested\n", encoding="utf-8")
    except OSError as exc:
        raise HTTPException(
            status_code=503,
            detail="No se pudo registrar la solicitud manual de actualización",
        ) from exc
    try:
        return await _iniciar_mantenimiento("fdeznet-update.service")
    except Exception:
        MANUAL_UPDATE_REQUEST_FILE.unlink(missing_ok=True)
        raise


@router.post("/mantenimiento/verificar", status_code=202)
async def iniciar_revision_recuperacion():
    return await _iniciar_mantenimiento("fdeznet-verify.service")


@router.get("/almacenamiento")
async def obtener_almacenamiento(db: AsyncSession = Depends(get_db)):
    config = await _obtener_configuracion(db)
    return await storage_dashboard(db, config)


@router.put("/almacenamiento/politica")
async def guardar_politica_almacenamiento(
    datos: StoragePolicyUpdate,
    db: AsyncSession = Depends(get_db),
):
    config = await _obtener_configuracion(db)
    config.limpieza_almacenamiento_automatica = datos.limpieza_automatica
    config.hora_limpieza_almacenamiento = datos.hora_limpieza
    config.dias_retencion_comprobantes_rechazados = datos.comprobantes_rechazados_dias
    config.dias_retencion_comprobantes_aprobados = datos.comprobantes_aprobados_dias
    config.dias_retencion_recibos_pdf = datos.recibos_pdf_dias
    config.dias_retencion_archivos_whatsapp = datos.archivos_whatsapp_dias
    config.dias_retencion_respaldos = datos.respaldos_dias
    config.cierre_mensual_automatico = datos.cierre_mensual_automatico
    config.dia_cierre_almacenamiento = datos.dia_cierre
    await db.commit()
    backup_updated = update_backup_retention(datos.respaldos_dias)
    return {
        "status": "ok",
        "mensaje": "Política de almacenamiento guardada",
        "retencion_respaldos_actualizada": backup_updated,
    }


@router.post("/almacenamiento/limpiar")
async def ejecutar_limpieza_almacenamiento(db: AsyncSession = Depends(get_db)):
    config = await _obtener_configuracion(db)
    return await cleanup_storage(db, config)


@router.post("/almacenamiento/cierres/{periodo}")
async def cerrar_periodo_almacenamiento(
    periodo: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    config = await _obtener_configuracion(db)
    try:
        return await close_period(
            db,
            config,
            periodo,
            closure_type="manual",
            user_id=current_user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# =========================================================
# 1. PLANTILLAS DE FACTURACIÓN (Ciclos de Cobro)
# =========================================================

@router.get("/plantillas-facturacion", response_model=List[PlantillaResponse])
async def listar_plantillas_facturacion(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(PlantillaFacturacionModel))
    return result.scalars().all()

@router.post("/plantillas-facturacion")
async def crear_plantilla_facturacion(data: BillingTemplateRequest, db: AsyncSession = Depends(get_db)):
    nueva = PlantillaFacturacionModel(**data.model_dump())
    db.add(nueva)
    await db.commit()
    await db.refresh(nueva)
    return nueva

@router.put("/plantillas-facturacion/{id}")
async def actualizar_plantilla_facturacion(id: int, data: BillingTemplateRequest, db: AsyncSession = Depends(get_db)):
    plantilla = await db.get(PlantillaFacturacionModel, id)
    if not plantilla:
        raise HTTPException(status_code=404, detail="Plantilla no encontrada")
    
    for key, value in data.model_dump().items():
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
@cache(expire=300)
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
@cache(expire=300)
async def obtener_configuracion_sistema(db: AsyncSession = Depends(get_db)):
    return await get_or_create_system_config(db)

@router.put("/sistema")
async def guardar_configuracion_sistema(datos: SystemConfigUpdate, db: AsyncSession = Depends(get_db)):
    stmt = update(ConfiguracionSistema).where(ConfiguracionSistema.id == 1).values(**datos.model_dump())
    await db.execute(stmt)
    await db.commit()
    return {"status": "ok", "mensaje": "Configuración guardada"}


# =========================================================
# 4. PPPOE DEFAULT (Legacy)
# =========================================================

@router.get("/pppoe-default")
@cache(expire=300)
async def obtener_password_default(db: AsyncSession = Depends(get_db)):
    stmt = select(ConfiguracionModel).where(ConfiguracionModel.clave == 'pppoe_password_default')
    res = await db.execute(stmt)
    config = res.scalar()
    return {"password": config.valor if config else None}

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
