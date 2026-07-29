"""evita facturas duplicadas en el mismo periodo de servicio

Revision ID: e4f5a6b7c8d9
Revises: d3f4a5b6c7d8
Create Date: 2026-07-28
"""
from alembic import op


revision = "e4f5a6b7c8d9"
down_revision = "d3f4a5b6c7d8"
branch_labels = None
depends_on = None


def upgrade():
    op.create_unique_constraint(
        "uq_factura_servicio_periodo",
        "facturas",
        ["servicio_id", "periodo_desde", "periodo_hasta"],
    )


def downgrade():
    op.drop_constraint(
        "uq_factura_servicio_periodo",
        "facturas",
        type_="unique",
    )
