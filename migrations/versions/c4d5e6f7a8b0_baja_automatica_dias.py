"""agrega dias para baja automatica por falta de pago

Revision ID: c4d5e6f7a8b0
Revises: b3c4d5e6f7a9
Create Date: 2026-09-27
"""

from alembic import op
import sqlalchemy as sa


revision = "c4d5e6f7a8b0"
down_revision = "b3c4d5e6f7a9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "configuracion_sistema",
        sa.Column(
            "baja_automatica_dias",
            sa.Integer(),
            nullable=False,
            server_default="90",
        ),
    )


def downgrade() -> None:
    op.drop_column("configuracion_sistema", "baja_automatica_dias")
