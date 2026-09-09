"""agrega tokens de instalacion de un solo uso

Revision ID: f9a0b1c2d3e4
Revises: e8f9a0b1c2d3
Create Date: 2026-09-08
"""

from alembic import op
import sqlalchemy as sa


revision = "f9a0b1c2d3e4"
down_revision = "e8f9a0b1c2d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("instalaciones_sistema", sa.Column("bootstrap_hash", sa.String(64), nullable=True))
    op.add_column("instalaciones_sistema", sa.Column("bootstrap_expira", sa.DateTime(), nullable=True))
    op.add_column("instalaciones_sistema", sa.Column("bootstrap_usado_en", sa.DateTime(), nullable=True))
    op.create_unique_constraint("uq_instalaciones_bootstrap_hash", "instalaciones_sistema", ["bootstrap_hash"])


def downgrade() -> None:
    op.drop_constraint("uq_instalaciones_bootstrap_hash", "instalaciones_sistema", type_="unique")
    op.drop_column("instalaciones_sistema", "bootstrap_usado_en")
    op.drop_column("instalaciones_sistema", "bootstrap_expira")
    op.drop_column("instalaciones_sistema", "bootstrap_hash")
