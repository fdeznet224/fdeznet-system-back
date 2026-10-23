"""Nombres de los equipos de la red del cliente

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-10-23
"""

from alembic import op
import sqlalchemy as sa


revision = "d2e3f4a5b6c7"
down_revision = "c1d2e3f4a5b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "equipos_nombres",
        sa.Column("cliente_id", sa.Integer, sa.ForeignKey("clientes.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("mac", sa.String(17), primary_key=True),
        sa.Column("nombre", sa.String(40), nullable=False),
        sa.Column("actualizado_en", sa.DateTime, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("equipos_nombres")
