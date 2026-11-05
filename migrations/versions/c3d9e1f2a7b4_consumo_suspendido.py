"""Consumo del día que fue estando suspendido (solo WhatsApp)

Revision ID: c3d9e1f2a7b4
Revises: 172c61be56e7
Create Date: 2026-11-03
"""

from alembic import op
import sqlalchemy as sa


revision = "c3d9e1f2a7b4"
down_revision = "172c61be56e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("consumo_diario",
                  sa.Column("suspendido_bytes", sa.BigInteger, nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("consumo_diario", "suspendido_bytes")
