"""mínimo de ONU en bodega para avisar cuando se están acabando

Revision ID: 2d7e8f9a0b1c
Revises: 1b5c6d7e8f9a
Create Date: 2026-10-06
"""

from alembic import op
import sqlalchemy as sa


revision = "2d7e8f9a0b1c"
down_revision = "1b5c6d7e8f9a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "configuracion_sistema",
        sa.Column("stock_minimo_onus", sa.Integer(), nullable=False, server_default="5"),
    )


def downgrade() -> None:
    op.drop_column("configuracion_sistema", "stock_minimo_onus")
