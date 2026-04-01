import os
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base

# 1. Cargar variables de entorno desde el archivo .env
load_dotenv()

# 2. Obtener credenciales (con valores de respaldo por si acaso)
DB_USER = os.getenv("DB_USER", "admin_isp")
DB_PASSWORD = os.getenv("DB_PASSWORD", "fdeznet224")
DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = os.getenv("DB_PORT", "3306")
DB_NAME = os.getenv("DB_NAME", "fdeznet_system")
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")

# 3. Construir la cadena de conexión dinámicamente
DATABASE_URL = f"mysql+asyncmy://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}?charset=utf8mb4"

# 4. Motor de Base de Datos Asíncrono
# Si ENVIRONMENT es "production", echo será False (limpia la consola en la VPS)
engine = create_async_engine(
    DATABASE_URL,
    echo=(ENVIRONMENT == "development"), 
    pool_pre_ping=True # Vital para que no se corte la conexión inactiva
)

# 5. Fábrica de Sesiones
SessionLocal = sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False
)

# 6. Clase Base para tus Modelos ORM
Base = declarative_base()

# 7. Dependency Injection (Para usar en los endpoints de FastAPI)
async def get_db():
    async with SessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()