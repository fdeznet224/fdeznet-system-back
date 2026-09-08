"""agrega periodicidad a servicios adicionales

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
Create Date: 2026-09-07
"""

from alembic import op
import sqlalchemy as sa


revision = "c6d7e8f9a0b1"
down_revision = "b5c6d7e8f9a0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "servicios_adicionales",
        sa.Column("periodicidad", sa.String(20), nullable=False, server_default="mensual"),
    )


def downgrade() -> None:
    op.drop_column("servicios_adicionales", "periodicidad")
