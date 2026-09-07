"""audita origen de promesas y reactivaciones

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0
Create Date: 2026-09-06
"""

from alembic import op
import sqlalchemy as sa


revision = "d6e7f8a9b0c1"
down_revision = "c5d6e7f8a9b0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "promesas_pago_historial",
        sa.Column("origen", sa.String(20), server_default="manual", nullable=False),
    )
    op.add_column(
        "promesas_pago_historial",
        sa.Column("servicio_reactivado", sa.Boolean(), server_default="0", nullable=False),
    )
    op.add_column(
        "promesas_pago_historial",
        sa.Column("reactivado_en", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "servicios",
        sa.Column("ultima_reactivacion_origen", sa.String(20), nullable=True),
    )
    op.add_column(
        "servicios",
        sa.Column("ultima_reactivacion_en", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("servicios", "ultima_reactivacion_en")
    op.drop_column("servicios", "ultima_reactivacion_origen")
    op.drop_column("promesas_pago_historial", "reactivado_en")
    op.drop_column("promesas_pago_historial", "servicio_reactivado")
    op.drop_column("promesas_pago_historial", "origen")
