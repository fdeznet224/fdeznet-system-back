"""forma de cobro en plantillas: día fijo o día de instalación

Revision ID: e6f7a8b9c0d2
Revises: d5e6f7a8b9c1
Create Date: 2026-09-29
"""

from alembic import op
import sqlalchemy as sa


revision = "e6f7a8b9c0d2"
down_revision = "d5e6f7a8b9c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "plantillas_facturacion",
        sa.Column(
            "ciclo_facturacion",
            sa.String(20),
            nullable=False,
            server_default="calendario",
        ),
    )


def downgrade() -> None:
    op.drop_column("plantillas_facturacion", "ciclo_facturacion")
