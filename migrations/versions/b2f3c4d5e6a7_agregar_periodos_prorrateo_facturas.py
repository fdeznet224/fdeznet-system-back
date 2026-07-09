"""agregar periodos y prorrateo a facturas

Revision ID: b2f3c4d5e6a7
Revises: 2316a59f3ffa
Create Date: 2026-07-08 22:55:37.315674
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b2f3c4d5e6a7"
down_revision: Union[str, None] = "2316a59f3ffa"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "facturas",
        sa.Column("periodo_desde", sa.Date(), nullable=True),
    )
    op.add_column(
        "facturas",
        sa.Column("periodo_hasta", sa.Date(), nullable=True),
    )
    op.add_column(
        "facturas",
        sa.Column("dias_facturados", sa.Integer(), nullable=True),
    )
    op.add_column(
        "facturas",
        sa.Column("dias_periodo", sa.Integer(), nullable=True),
    )
    op.add_column(
        "facturas",
        sa.Column("precio_mensual_snapshot", sa.Float(), nullable=True),
    )
    op.add_column(
        "facturas",
        sa.Column("precio_diario", sa.Float(), nullable=True),
    )
    op.add_column(
        "facturas",
        sa.Column(
            "es_prorrateada",
            sa.Boolean(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "facturas",
        sa.Column("tipo_facturacion_snapshot", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "facturas",
        sa.Column("ciclo_facturacion_snapshot", sa.String(length=20), nullable=True),
    )
    op.create_index(
        "ix_facturas_servicio_periodo",
        "facturas",
        ["servicio_id", "periodo_desde", "periodo_hasta"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_facturas_servicio_periodo",
        table_name="facturas",
    )
    op.drop_column("facturas", "ciclo_facturacion_snapshot")
    op.drop_column("facturas", "tipo_facturacion_snapshot")
    op.drop_column("facturas", "es_prorrateada")
    op.drop_column("facturas", "precio_diario")
    op.drop_column("facturas", "precio_mensual_snapshot")
    op.drop_column("facturas", "dias_periodo")
    op.drop_column("facturas", "dias_facturados")
    op.drop_column("facturas", "periodo_hasta")
    op.drop_column("facturas", "periodo_desde")
