# Ubicación: backend/src/infrastructure/database.py
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base

# CADENA DE CONEXIÓN (Local)
# Sintaxis: mysql+asyncmy://USUARIO:PASSWORD@HOST:PUERTO/NOMBRE_BD?charset=utf8mb4
# NOTA: Agregamos ?charset=utf8mb4 al final para soportar emojis ✅
DATABASE_URL = "mysql+asyncmy://admin_isp:fdeznet224@127.0.0.1:3306/fdeznet_system?charset=utf8mb4"

# Motor de Base de Datos Asíncrono
engine = create_async_engine(
    DATABASE_URL,
    echo=True, # Ponlo en False cuando vayas a producción para limpiar la consola
    pool_pre_ping=True # Vital para que no se corte la conexión inactiva
)

# Fábrica de Sesiones (Cada petición tendrá su propia sesión)
SessionLocal = sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False
)

# Clase Base para tus Modelos ORM
Base = declarative_base()

# Dependency Injection (Para usar en los endpoints de FastAPI)
async def get_db():
    async with SessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()