"""cliente identificado en su caja NAP con el láser y conector etiquetado

Revision ID: 3e8f9a0b1c2d
Revises: 2d7e8f9a0b1c
Create Date: 2026-10-10
"""

from alembic import op
import sqlalchemy as sa


revision = "3e8f9a0b1c2d"
down_revision = "2d7e8f9a0b1c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("clientes", sa.Column("nap_identificado_en", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "clientes",
        sa.Column("conector_etiquetado", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("clientes", "conector_etiquetado")
    op.drop_column("clientes", "nap_identificado_en")
