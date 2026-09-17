"""agrega subredes remotas a tuneles vpn

Revision ID: a2b3c4d5e6f8
Revises: f2a3b4c5d6e7
Create Date: 2026-09-16
"""

from alembic import op
import sqlalchemy as sa


revision = "a2b3c4d5e6f8"
down_revision = "f2a3b4c5d6e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("vpn_tunnels", sa.Column("subredes_remotas", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("vpn_tunnels", "subredes_remotas")
