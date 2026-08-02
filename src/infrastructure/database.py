import os
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.engine import URL

# 1. Cargar variables de entorno desde el archivo .env
load_dotenv()

# 2. Obtener configuración. En producción no se permiten credenciales implícitas.
DB_USER = os.getenv("DB_USER", "")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = os.getenv("DB_PORT", "3306")
DB_NAME = os.getenv("DB_NAME", "")
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
DATABASE_URL_ENV = os.getenv("DATABASE_URL", "").strip()

if ENVIRONMENT.lower() == "production" and not DATABASE_URL_ENV and not all((DB_USER, DB_PASSWORD, DB_NAME)):
    raise RuntimeError(
        "Configura DATABASE_URL o DB_USER, DB_PASSWORD y DB_NAME en producción"
    )

# Valores locales explícitamente no productivos, útiles para pruebas/desarrollo.
DB_USER = DB_USER or "fdeznet_dev"
DB_NAME = DB_NAME or "fdeznet_dev"

# URL.create escapa correctamente contraseñas con @, /, : u otros caracteres.
DATABASE_URL = DATABASE_URL_ENV or URL.create(
    "mysql+asyncmy",
    username=DB_USER,
    password=DB_PASSWORD or None,
    host=DB_HOST,
    port=int(DB_PORT),
    database=DB_NAME,
    query={"charset": "utf8mb4"},
)

# 4. Motor de Base de Datos Asíncrono
# Si ENVIRONMENT es "production", echo será False (limpia la consola en la VPS)
engine = create_async_engine(
    DATABASE_URL,
    echo=(ENVIRONMENT == "development"), 
    # asyncmy 0.2.x no implementa la firma que SQLAlchemy espera para
    # pool_pre_ping; pool_recycle evita conexiones inactivas obsoletas.
    pool_pre_ping=False,
    pool_recycle=int(os.getenv("DB_POOL_RECYCLE", "1800")),
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
