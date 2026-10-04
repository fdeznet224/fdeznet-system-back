"""mensaje del cliente que llegó mientras el bot estaba pausado

Revision ID: 7d1e2a3b4c5f
Revises: 5b4a39281706
Create Date: 2026-10-03
"""

from alembic import op
import sqlalchemy as sa


revision = "7d1e2a3b4c5f"
down_revision = "5b4a39281706"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("bot_pausas") as tabla:
        tabla.add_column(sa.Column("mensaje_pendiente_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("bot_pausas") as tabla:
        tabla.drop_column("mensaje_pendiente_id")
