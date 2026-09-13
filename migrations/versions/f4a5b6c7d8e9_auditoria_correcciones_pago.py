"""auditoria para correcciones de pagos

Revision ID: f4a5b6c7d8e9
Revises: e0f1a2b3c4d5
Create Date: 2026-09-12
"""
from alembic import op
import sqlalchemy as sa


revision = "f4a5b6c7d8e9"
down_revision = "e0f1a2b3c4d5"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "pagos",
        sa.Column("pago_origen_correccion_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "pagos",
        sa.Column("corregido_por_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "pagos",
        sa.Column("motivo_correccion", sa.String(length=500), nullable=True),
    )
    op.create_foreign_key(
        "fk_pago_origen_correccion",
        "pagos",
        "pagos",
        ["pago_origen_correccion_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_pago_corregido_por",
        "pagos",
        "usuarios",
        ["corregido_por_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_pagos_pago_origen_correccion_id",
        "pagos",
        ["pago_origen_correccion_id"],
        unique=True,
    )


def downgrade():
    op.drop_index("ix_pagos_pago_origen_correccion_id", table_name="pagos")
    op.drop_constraint("fk_pago_corregido_por", "pagos", type_="foreignkey")
    op.drop_constraint("fk_pago_origen_correccion", "pagos", type_="foreignkey")
    op.drop_column("pagos", "motivo_correccion")
    op.drop_column("pagos", "corregido_por_id")
    op.drop_column("pagos", "pago_origen_correccion_id")
