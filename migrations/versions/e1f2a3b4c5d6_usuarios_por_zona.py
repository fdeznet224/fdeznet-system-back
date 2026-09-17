"""permite asignar cobradores y personal a zonas

Revision ID: e1f2a3b4c5d6
Revises: d6e7f8a9b0c1
Create Date: 2026-09-16
"""

from alembic import op
import sqlalchemy as sa


revision = "e1f2a3b4c5d6"
down_revision = "d6e7f8a9b0c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "usuario_zonas",
        sa.Column("usuario_id", sa.Integer(), nullable=False),
        sa.Column("zona_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["usuario_id"], ["usuarios.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["zona_id"], ["zonas.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("usuario_id", "zona_id"),
    )


def downgrade() -> None:
    op.drop_table("usuario_zonas")
