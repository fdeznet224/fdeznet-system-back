"""agregar auditoria para anulacion de facturas

Revision ID: a3b4c5d6e7f8
Revises: f2c3d4e5f6a7
Create Date: 2026-08-27
"""
from alembic import op
import sqlalchemy as sa


revision = "a3b4c5d6e7f8"
down_revision = "f2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "facturas",
        sa.Column("motivo_anulacion", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "facturas",
        sa.Column("anulada_por_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "facturas",
        sa.Column("fecha_anulacion", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "facturas",
        sa.Column("saldo_antes_anulacion", sa.Numeric(12, 2), nullable=True),
    )
    op.create_foreign_key(
        "fk_facturas_anulada_por_id_usuarios",
        "facturas",
        "usuarios",
        ["anulada_por_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade():
    op.drop_constraint(
        "fk_facturas_anulada_por_id_usuarios",
        "facturas",
        type_="foreignkey",
    )
    op.drop_column("facturas", "saldo_antes_anulacion")
    op.drop_column("facturas", "fecha_anulacion")
    op.drop_column("facturas", "anulada_por_id")
    op.drop_column("facturas", "motivo_anulacion")
