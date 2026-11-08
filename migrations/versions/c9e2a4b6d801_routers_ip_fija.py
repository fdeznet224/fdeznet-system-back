"""Routers con IP fija

routers.tipo_seguridad gana "estatica": el cliente trae la IP puesta en su
equipo y el MikroTik solo le pone la velocidad con una cola simple.

Revision ID: c9e2a4b6d801
Revises: b8d4f1e3a520
Create Date: 2026-11-08
"""

import sqlalchemy as sa
from alembic import op

revision = "c9e2a4b6d801"
down_revision = "b8d4f1e3a520"
branch_labels = None
depends_on = None

ANTES = sa.Enum("pppoe", "dhcp", name="tiposeguridad")
DESPUES = sa.Enum("pppoe", "dhcp", "estatica", name="tiposeguridad")


def upgrade():
    with op.batch_alter_table("routers") as tabla:
        tabla.alter_column("tipo_seguridad", existing_type=ANTES, type_=DESPUES, existing_nullable=True)


def downgrade():
    op.execute("UPDATE routers SET tipo_seguridad = 'pppoe' WHERE tipo_seguridad = 'estatica'")
    with op.batch_alter_table("routers") as tabla:
        tabla.alter_column("tipo_seguridad", existing_type=DESPUES, type_=ANTES, existing_nullable=True)
