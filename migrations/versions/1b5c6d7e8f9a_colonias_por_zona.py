"""cada zona guarda las colonias que cubre

Revision ID: 1b5c6d7e8f9a
Revises: 0a4b5c6d7e8f
Create Date: 2026-10-04
"""

from alembic import op
import sqlalchemy as sa


revision = "1b5c6d7e8f9a"
down_revision = "0a4b5c6d7e8f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("zonas", sa.Column("colonias", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("zonas", "colonias")
