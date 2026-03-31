import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

# Scheduler para Cronjobs
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from src.jobs import tarea_cron_unificada

# Base de Datos
from src.infrastructure.database import engine, Base, SessionLocal
from src.infrastructure import models
from src.infrastructure.models import UsuarioModel 

# Servicios y Schemas
from src.application.services.user_service import UserService
from src.domain.schemas import UsuarioCreate

# Importar Routers
# 👇 AGREGAMOS 'chat' AQUÍ
from src.interfaces.api import (
    auth, clients, planes, finanzas, network,          
    zonas, usuarios, configuracion, dashboard,
    whatsapp, naps,vpn
)

# ==========================================
# ⚙️ CONFIGURACIÓN DEL CICLO DE VIDA (LIFESPAN)
# ==========================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 Iniciando FdezNet System...")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("✅ Base de Datos Sincronizada")

    async with SessionLocal() as db:
        try:
            stmt = select(UsuarioModel).where(UsuarioModel.usuario == "admin")
            result = await db.execute(stmt)
            if not result.scalar_one_or_none():
                print("👤 Creando Super Admin por defecto...")
                service = UserService(db)
                admin_data = UsuarioCreate(
                    nombre_completo="Super Administrador",
                    usuario="admin",
                    password="admin123",
                    rol="admin",
                    activo=True,
                    router_ids=[]
                )
                await service.crear_usuario(admin_data)
                print("✅ Admin creado (user: admin / pass: admin123)")
        except Exception as e:
            print(f"⚠️ Error verificando admin: {e}")

    print("⏳ Iniciando Planificador de Tareas Automáticas...")
    scheduler = AsyncIOScheduler()
    scheduler.add_job(tarea_cron_unificada, 'cron', minute=0)
    scheduler.start()
    print("✅ Planificador Activo.")

    yield 

    print("🛑 Apagando Planificador...")
    scheduler.shutdown()
    print("👋 FdezNet System detenido.")


# ==========================================
# 🚀 INICIALIZACIÓN DE LA APP
# ==========================================
app = FastAPI(
    title="FdezNet System", 
    version="2.2.0 Real-time Chat Ready",
    lifespan=lifespan,
    root_path="/api"  # 👈 ¡ESTA LÍNEA ES MAGIA PARA NGINX!
)

if not os.path.exists("static/recibos"):
    os.makedirs("static/recibos", exist_ok=True)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # En producción cambia "*" por tu dominio real
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- RUTAS ---
app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(clients.router)       
app.include_router(planes.router)        
app.include_router(finanzas.router)
app.include_router(network.router)          
app.include_router(usuarios.router)
app.include_router(zonas.router)
app.include_router(configuracion.router) 
app.include_router(whatsapp.router)
app.include_router(naps.router)
app.include_router(vpn.router)

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def home():
    return {
        "mensaje": "API FdezNet Arquitectura Limpia Activa 🚀",
        "cronjob": "Activo (Revisión cada hora)",
        "chat_system": "WebSocket Online",
        "version": "2.2.0"
    }