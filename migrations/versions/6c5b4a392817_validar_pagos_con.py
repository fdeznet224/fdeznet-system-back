"""cómo se confirman los pagos: con el correo del banco o solo con la captura

Revision ID: 6c5b4a392817
Revises: 7d6c5b4a3928
Create Date: 2026-09-30
"""

from alembic import op
import sqlalchemy as sa


revision = "6c5b4a392817"
down_revision = "7d6c5b4a3928"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("configuracion_correo_banco") as tabla:
        tabla.add_column(
            sa.Column("validar_pagos_con", sa.String(length=20), nullable=False, server_default="correo")
        )


def downgrade() -> None:
    with op.batch_alter_table("configuracion_correo_banco") as tabla:
        tabla.drop_column("validar_pagos_con")
