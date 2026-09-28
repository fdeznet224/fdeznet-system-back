"""pausa del bot por chat cuando atiende un asesor

Revision ID: d5e6f7a8b9c1
Revises: c4d5e6f7a8b0
Create Date: 2026-09-28
"""

from alembic import op
import sqlalchemy as sa


revision = "d5e6f7a8b9c1"
down_revision = "c4d5e6f7a8b0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bot_pausas",
        sa.Column("telefono", sa.String(20), primary_key=True),
        sa.Column("pausado_hasta", sa.DateTime(), nullable=False),
        sa.Column("motivo", sa.String(40), nullable=False),
        sa.Column("actualizado_en", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("bot_pausas")
