"""persiste el intervalo individual de la cola de WhatsApp

Revision ID: f1b2c3d4e5f6
Revises: f0a1b2c3d4e5
Create Date: 2026-08-01
"""

from alembic import op
import sqlalchemy as sa


revision = "f1b2c3d4e5f6"
down_revision = "f0a1b2c3d4e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "mensajes_chat",
        sa.Column("intervalo_salida", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("mensajes_chat", "intervalo_salida")
