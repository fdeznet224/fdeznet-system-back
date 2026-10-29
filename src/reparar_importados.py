"""Crea el servicio de los clientes importados que quedaron sin él (importador viejo).

    python -m src.reparar_importados
"""
import asyncio

from src.application.services.importacion_service import reparar_importados_sin_servicio
from src.infrastructure.database import SessionLocal


async def main() -> None:
    async with SessionLocal() as db:
        print(f"SERVICIOS_CREADOS={await reparar_importados_sin_servicio(db)}")


if __name__ == "__main__":
    asyncio.run(main())
