"""amplia promesas y las limita al siguiente ciclo de pago

Revision ID: f2c3d4e5f6a7
Revises: f1b2c3d4e5f6
Create Date: 2026-08-01
"""

from alembic import op
import sqlalchemy as sa


revision = "f2c3d4e5f6a7"
down_revision = "f1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 25 días es el límite general. La validación también lo recorta al día
    # anterior al siguiente vencimiento de la factura.
    op.execute(
        sa.text(
            "UPDATE politicas_cobranza "
            "SET dias_max_promesa = 25 "
            "WHERE dias_max_promesa = 7"
        )
    )
    op.alter_column(
        "politicas_cobranza",
        "dias_max_promesa",
        server_default="25",
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE politicas_cobranza "
            "SET dias_max_promesa = 7 "
            "WHERE dias_max_promesa = 25"
        )
    )
    op.alter_column(
        "politicas_cobranza",
        "dias_max_promesa",
        server_default="7",
    )
