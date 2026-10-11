"""hora del aviso diario de señal óptica, editable (antes fija a las 2 a. m.)

Revision ID: 4f9a0b1c2d3e
Revises: 3e8f9a0b1c2d
Create Date: 2026-10-10
"""

from alembic import op
import sqlalchemy as sa


revision = "4f9a0b1c2d3e"
down_revision = "3e8f9a0b1c2d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "configuracion_sistema",
        sa.Column("hora_aviso_senal", sa.String(10), nullable=False, server_default="08:00"),
    )


def downgrade() -> None:
    op.drop_column("configuracion_sistema", "hora_aviso_senal")
