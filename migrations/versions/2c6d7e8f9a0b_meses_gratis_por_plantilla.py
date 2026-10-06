"""cada plantilla de cobro define los meses gratis de una instalación nueva

Revision ID: 2c6d7e8f9a0b
Revises: 1b5c6d7e8f9a
Create Date: 2026-10-05
"""

from alembic import op
import sqlalchemy as sa


revision = "2c6d7e8f9a0b"
down_revision = "1b5c6d7e8f9a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "plantillas_facturacion",
        sa.Column(
            "meses_gratis_instalacion",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("plantillas_facturacion", "meses_gratis_instalacion")
