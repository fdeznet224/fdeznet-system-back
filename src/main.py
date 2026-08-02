import os
import logging
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from fastapi_cache import FastAPICache
from fastapi_cache.backends.inmemory import InMemoryBackend

# Scheduler para Cronjobs
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from src.jobs import (
    tarea_conciliar_mikrotik,
    tarea_cron_unificada,
    tarea_monitoreo_routers,
    tarea_sincronizar_clientes,
)

# Base de Datos
from src.infrastructure.database import engine, Base, SessionLocal
from src.infrastructure.models import UsuarioModel
from src.infrastructure.auth import get_current_active_user, role_required
from src.infrastructure.audit_middleware import AuditMiddleware
from src.infrastructure.whatsapp_client import whatsapp_queue

# Servicios y Schemas
from src.application.services.user_service import UserService
from src.domain.schemas import UsuarioCreate

# Importar Routers
from src.interfaces.api import (
    auditoria,
    auth,
    bajas,
    clients,
    configuracion,
    dashboard,
    finanzas,
    ftth,
    inventario,
    naps,
    network,
    olts,
    ordenes,
    planes,
    servicios,
    support,
    sync,
    usuarios,
    vpn,
    whatsapp,
    zonas,
)

logger = logging.getLogger(__name__)
ENVIRONMENT = os.getenv("ENVIRONMENT", "development").strip().lower()


def _cors_origins():
    configured = os.getenv("CORS_ALLOWED_ORIGINS", "")
    origins = [item.strip() for item in configured.split(",") if item.strip()]
    if origins:
        if "*" in origins:
            raise RuntimeError("CORS_ALLOWED_ORIGINS no puede contener '*'")
        return origins
    if ENVIRONMENT == "production":
        raise RuntimeError("CORS_ALLOWED_ORIGINS es obligatoria en producción")
    return ["http://localhost:3000", "http://localhost:5173"]

# ==========================================
# ⚙️ CONFIGURACIÓN DEL CICLO DE VIDA
# ==========================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 Iniciando FdezNet System...")

    # 1. INICIALIZAR CACHÉ
    FastAPICache.init(InMemoryBackend(), prefix="fdeznet-cache")
    print("⚡ Caché en memoria RAM inicializado")

    # 2. SINCRONIZAR BASE DE DATOS
    # En producción el esquema debe avanzar exclusivamente con Alembic.
    # create_all puede ocultar migraciones faltantes y generar drift entre nodos.
    if ENVIRONMENT != "production":
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        print("✅ Base de Datos Sincronizada (modo desarrollo)")
    else:
        print("✅ Esquema de producción administrado por Alembic")

    # Recupera mensajes de salida que quedaron sin ACK antes de un reinicio.
    await whatsapp_queue.recuperar_pendientes()

    # 3. CREAR ADMIN SOLO CUANDO SE PROPORCIONAN CREDENCIALES DE BOOTSTRAP
    async with SessionLocal() as db:
        try:
            bootstrap_user = os.getenv("ADMIN_BOOTSTRAP_USER", "admin").strip()
            bootstrap_password = os.getenv("ADMIN_BOOTSTRAP_PASSWORD", "")
            stmt = select(UsuarioModel).where(UsuarioModel.rol == "admin")
            result = await db.execute(stmt)
            if not result.scalar_one_or_none():
                if bootstrap_password:
                    service = UserService(db)
                    admin_data = UsuarioCreate(
                        nombre_completo=os.getenv(
                            "ADMIN_BOOTSTRAP_NAME",
                            "Super Administrador",
                        ),
                        usuario=bootstrap_user,
                        password=bootstrap_password,
                        rol="admin",
                        activo=True,
                        router_ids=[],
                    )
                    await service.crear_usuario(admin_data)
                    logger.warning(
                        "Administrador inicial creado; elimina "
                        "ADMIN_BOOTSTRAP_PASSWORD del entorno"
                    )
                elif ENVIRONMENT == "production":
                    raise RuntimeError(
                        "No existe un administrador. Configura temporalmente "
                        "ADMIN_BOOTSTRAP_PASSWORD para crear el primero."
                    )
                else:
                    logger.warning(
                        "No existe un administrador y el bootstrap está desactivado"
                    )
        except Exception:
            logger.exception("Error verificando el administrador inicial")
            raise

    # 4. INICIAR CRONJOBS
    print("⏳ Iniciando Planificador de Tareas Automáticas...")
    scheduler = AsyncIOScheduler()
    
    # Monitoreo (1 min), Clientes (3 min), Facturación/Cortes (1 hora/día)
    scheduler.add_job(tarea_monitoreo_routers, 'interval', minutes=1)
    scheduler.add_job(tarea_sincronizar_clientes, 'interval', minutes=3)
    scheduler.add_job(
        tarea_conciliar_mikrotik,
        "interval",
        minutes=5,
        id="mikrotik_state_reconciler",
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        whatsapp_queue.recuperar_pendientes,
        "interval",
        minutes=1,
        id="whatsapp_outbox_dispatcher",
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        tarea_cron_unificada,
        "interval",
        minutes=1,
        id="cron_unificado",
        coalesce=True,
        max_instances=1,
    )
    
    scheduler.start()
    print("✅ Planificador Activo (Facturación, Red, Estados).")

    yield # --- LA APP CORRE AQUÍ ---

    print("🛑 Apagando Planificador...")
    scheduler.shutdown()
    print("👋 FdezNet System detenido.")


# ==========================================
# 🚀 INSTANCIA DE LA APP
# ==========================================
app = FastAPI(
    title="FdezNet System", 
    version="2.2.0 Real-time System",
    lifespan=lifespan,
    root_path="/api"
)

if not os.path.exists("static/recibos"):
    os.makedirs("static/recibos", exist_ok=True)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "X-Webhook-Secret",
        "Cache-Control",
        "Pragma",
        "Expires",
    ],
)
app.add_middleware(AuditMiddleware)

# --- RUTAS ---
app.include_router(auth.router)
app.include_router(whatsapp.webhook_router)

authenticated = [Depends(get_current_active_user)]
admin_only = [Depends(role_required(["admin"]))]
audit_readers = [Depends(role_required(["admin", "supervisor"]))]
financial_roles = [Depends(role_required(["admin", "supervisor", "cajero"]))]

app.include_router(dashboard.router, dependencies=authenticated)
app.include_router(clients.router, dependencies=authenticated)
app.include_router(planes.router, dependencies=authenticated)
app.include_router(servicios.router, dependencies=authenticated)
app.include_router(finanzas.router, dependencies=financial_roles)
app.include_router(network.router, dependencies=authenticated)
app.include_router(usuarios.router, dependencies=admin_only)
app.include_router(zonas.router, dependencies=authenticated)
app.include_router(configuracion.router, dependencies=admin_only)
app.include_router(whatsapp.router, dependencies=authenticated)
app.include_router(naps.router, dependencies=authenticated)
app.include_router(vpn.router, dependencies=admin_only)
app.include_router(olts.router, dependencies=authenticated)
app.include_router(inventario.router, dependencies=authenticated)
app.include_router(auditoria.router, dependencies=audit_readers)
app.include_router(ordenes.router, dependencies=authenticated)
app.include_router(ftth.router, dependencies=authenticated)
app.include_router(support.router, dependencies=authenticated)
app.include_router(sync.router, dependencies=authenticated)
app.include_router(bajas.router, dependencies=authenticated)

@app.get("/")
def home():
    return {
        "status": "online",
        "system": "FdezNet v2.2.0",
        "cron_status": "active"
    }


@app.get("/health/live", tags=["system"])
def health_live():
    """Confirma que el proceso HTTP está atendiendo solicitudes."""
    return {"status": "ok"}


@app.get("/health/ready", tags=["system"])
async def health_ready():
    """Confirma que la aplicación puede consultar su base de datos."""
    try:
        async with SessionLocal() as db:
            await db.execute(select(1))
    except Exception as exc:
        logger.exception("La comprobación de disponibilidad falló")
        raise HTTPException(
            status_code=503,
            detail="Base de datos no disponible",
        ) from exc
    return {"status": "ready"}
