"""Prepara la base de un ISP nuevo igual que install.sh; imprime fresh o existing."""
import asyncio

from sqlalchemy import inspect, text

from src.infrastructure import models  # noqa: F401
from src.application.services.installation_seed_service import seed_fresh_installation
from src.infrastructure.database import Base, SessionLocal, engine


async def prepare_database() -> None:
    async with engine.begin() as connection:
        tables = await connection.run_sync(
            lambda sync_connection: set(inspect(sync_connection).get_table_names())
        )
        application_tables = tables - {"alembic_version"}

        if application_tables:
            if "clientes" not in application_tables:
                raise RuntimeError(
                    "La base contiene tablas parciales pero no la tabla clientes; "
                    "se requiere revisión antes de continuar"
                )
            print("existing")
            return

        # La primera revisión de Alembic es una línea base histórica y supone
        # que estas tablas ya existen. En una VPS nueva se crea el esquema del
        # release fijado y después Alembic lo registra en la revisión actual.
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(
            text(
                """
                INSERT INTO politicas_cobranza (
                    nombre, tipo_cliente, dias_max_promesa,
                    max_promesas_activas, max_incumplidas_90_dias,
                    permite_reconexion, activa
                )
                SELECT 'Residencial', 'residencial', 7, 1, 2, 1, 1
                WHERE NOT EXISTS (
                    SELECT 1 FROM politicas_cobranza
                    WHERE tipo_cliente = 'residencial'
                )
                """
            )
        )
    async with SessionLocal() as database:
        await seed_fresh_installation(database)
    print("fresh")


asyncio.run(prepare_database())
