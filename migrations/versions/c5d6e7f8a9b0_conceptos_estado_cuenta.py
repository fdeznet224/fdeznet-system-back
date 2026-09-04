"""conceptos independientes para estado de cuenta

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9
Create Date: 2026-09-03
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "c5d6e7f8a9b0"
down_revision = "b4c5d6e7f8a9"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if "factura_conceptos" in inspect(bind).get_table_names():
        return
    op.create_table(
        "factura_conceptos",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("factura_id", sa.Integer(), sa.ForeignKey("facturas.id", ondelete="CASCADE"), nullable=True),
        sa.Column("cliente_id", sa.Integer(), sa.ForeignKey("clientes.id"), nullable=False),
        sa.Column("servicio_id", sa.Integer(), sa.ForeignKey("servicios.id"), nullable=True),
        sa.Column("tipo", sa.String(30), nullable=False, server_default="cargo"),
        sa.Column("concepto", sa.String(150), nullable=False),
        sa.Column("descripcion", sa.String(500), nullable=True),
        sa.Column("monto_original", sa.Numeric(12, 2), nullable=False),
        sa.Column("saldo_pendiente", sa.Numeric(12, 2), nullable=False),
        sa.Column("estado", sa.String(20), nullable=False, server_default="pendiente"),
        sa.Column("afecta_corte", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("fecha_cargo", sa.Date(), nullable=False),
        sa.Column("numero_cuota", sa.Integer(), nullable=True),
        sa.Column("total_cuotas", sa.Integer(), nullable=True),
        sa.Column("cargo_origen_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ix_factura_conceptos_factura_id", "factura_conceptos", ["factura_id"])
    op.create_index("ix_factura_conceptos_cliente_id", "factura_conceptos", ["cliente_id"])
    op.create_index("ix_factura_conceptos_servicio_id", "factura_conceptos", ["servicio_id"])
    op.create_index("ix_factura_conceptos_cargo_origen_id", "factura_conceptos", ["cargo_origen_id"])
    op.create_table(
        "pago_conceptos",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("pago_id", sa.Integer(), sa.ForeignKey("pagos.id", ondelete="CASCADE"), nullable=False),
        sa.Column("concepto_id", sa.Integer(), sa.ForeignKey("factura_conceptos.id", ondelete="CASCADE"), nullable=False),
        sa.Column("monto_aplicado", sa.Numeric(12, 2), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ix_pago_conceptos_pago_id", "pago_conceptos", ["pago_id"])
    op.create_index("ix_pago_conceptos_concepto_id", "pago_conceptos", ["concepto_id"])


def downgrade():
    op.drop_table("pago_conceptos")
    op.drop_table("factura_conceptos")
