"""agregar prorrateo por suspension y consolidacion

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8
Create Date: 2026-08-27
"""
from alembic import op
import sqlalchemy as sa


revision = "b4c5d6e7f8a9"
down_revision = "a3b4c5d6e7f8"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "servicios",
        sa.Column("fecha_suspension_facturacion", sa.Date(), nullable=True),
    )
    op.add_column(
        "servicios",
        sa.Column("fecha_ultima_reactivacion", sa.Date(), nullable=True),
    )
    op.add_column(
        "facturas",
        sa.Column("monto_servicio_original", sa.Numeric(12, 2), nullable=True),
    )
    op.add_column(
        "facturas",
        sa.Column("impuesto_servicio_original", sa.Numeric(12, 2), nullable=True),
    )
    op.add_column(
        "facturas",
        sa.Column(
            "cargos_adicionales_total",
            sa.Numeric(12, 2),
            nullable=False,
            server_default=sa.text("0.00"),
        ),
    )
    op.add_column("facturas", sa.Column("dias_con_servicio", sa.Integer(), nullable=True))
    op.add_column("facturas", sa.Column("dias_sin_servicio", sa.Integer(), nullable=True))
    op.add_column(
        "facturas",
        sa.Column(
            "ajuste_suspension",
            sa.Numeric(12, 2),
            nullable=False,
            server_default=sa.text("0.00"),
        ),
    )
    op.create_table(
        "suspensiones_facturacion",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("servicio_id", sa.Integer(), nullable=False),
        sa.Column("factura_origen_id", sa.Integer(), nullable=True),
        sa.Column("fecha_inicio", sa.Date(), nullable=False),
        sa.Column("fecha_fin", sa.Date(), nullable=True),
        sa.Column("motivo_inicio", sa.String(length=100), nullable=False),
        sa.Column("motivo_fin", sa.String(length=100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["servicio_id"], ["servicios.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["factura_origen_id"], ["facturas.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_suspensiones_facturacion_servicio_id",
        "suspensiones_facturacion",
        ["servicio_id"],
    )
    op.create_index(
        "ix_suspensiones_facturacion_servicio_fechas",
        "suspensiones_facturacion",
        ["servicio_id", "fecha_inicio", "fecha_fin"],
    )
    op.execute(
        """
        UPDATE facturas
        SET monto_servicio_original = monto,
            impuesto_servicio_original = impuesto,
            dias_con_servicio = dias_facturados,
            dias_sin_servicio = 0
        WHERE tipo_factura IN ('mensual', 'prorrateo')
        """
    )
    op.execute(
        """
        UPDATE facturas
        SET cargos_adicionales_total = monto
        WHERE tipo_factura = 'manual'
        """
    )


def downgrade():
    op.drop_index(
        "ix_suspensiones_facturacion_servicio_fechas",
        table_name="suspensiones_facturacion",
    )
    op.drop_index(
        "ix_suspensiones_facturacion_servicio_id",
        table_name="suspensiones_facturacion",
    )
    op.drop_table("suspensiones_facturacion")
    op.drop_column("facturas", "ajuste_suspension")
    op.drop_column("facturas", "dias_sin_servicio")
    op.drop_column("facturas", "dias_con_servicio")
    op.drop_column("facturas", "cargos_adicionales_total")
    op.drop_column("facturas", "impuesto_servicio_original")
    op.drop_column("facturas", "monto_servicio_original")
    op.drop_column("servicios", "fecha_ultima_reactivacion")
    op.drop_column("servicios", "fecha_suspension_facturacion")
