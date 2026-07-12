"""ampliar mes_correspondiente en facturas

Revision ID: c9d8e7f6a5b4
Revises: b2f3c4d5e6a7
Create Date: 2026-07-08 23:30:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c9d8e7f6a5b4"
down_revision: Union[str, Sequence[str], None] = "b2f3c4d5e6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "facturas",
        "mes_correspondiente",
        existing_type=sa.String(length=20),
        type_=sa.String(length=100),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "facturas",
        "mes_correspondiente",
        existing_type=sa.String(length=100),
        type_=sa.String(length=20),
        existing_nullable=True,
    )
