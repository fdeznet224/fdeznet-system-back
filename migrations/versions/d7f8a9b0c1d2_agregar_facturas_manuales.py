"""agregar facturas manuales

Revision ID: d7f8a9b0c1d2
Revises: c9d8e7f6a5b4
Create Date: 2026-07-21
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text


revision = "d7f8a9b0c1d2"
down_revision = "c9d8e7f6a5b4"
branch_labels = None
depends_on = None


def _has_column(bind, table_name, column_name):
    inspector = inspect(bind)
    return column_name in [c["name"] for c in inspector.get_columns(table_name)]


def upgrade():
    bind = op.get_bind()

    if not _has_column(bind, "facturas", "tipo_factura"):
        op.add_column(
            "facturas",
            sa.Column("tipo_factura", sa.String(30), nullable=False, server_default="mensual"),
        )

    if not _has_column(bind, "facturas", "concepto"):
        op.add_column(
            "facturas",
            sa.Column("concepto", sa.String(150), nullable=True),
        )

    if not _has_column(bind, "facturas", "descripcion"):
        op.add_column(
            "facturas",
            sa.Column("descripcion", sa.String(500), nullable=True),
        )

    if not _has_column(bind, "facturas", "afecta_corte"):
        op.add_column(
            "facturas",
            sa.Column("afecta_corte", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        )

    if not _has_column(bind, "facturas", "creada_manual"):
        op.add_column(
            "facturas",
            sa.Column("creada_manual", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        )

    # Clasificar facturas existentes
    bind.execute(text("""
        UPDATE facturas
        SET tipo_factura = CASE
            WHEN es_prorrateada = 1 THEN 'prorrateo'
            ELSE 'mensual'
        END
        WHERE tipo_factura IS NULL OR tipo_factura = ''
    """))

    bind.execute(text("""
        UPDATE facturas
        SET concepto = CASE
            WHEN es_prorrateada = 1 THEN 'Prorrateo servicio de internet'
            ELSE 'Servicio mensual de internet'
        END
        WHERE concepto IS NULL OR concepto = ''
    """))

    bind.execute(text("""
        UPDATE facturas
        SET afecta_corte = 1
        WHERE afecta_corte IS NULL
    """))

    bind.execute(text("""
        UPDATE facturas
        SET creada_manual = 0
        WHERE creada_manual IS NULL
    """))

    # Corrección importante:
    # la promesa NO debe ser estado contable.
    bind.execute(text("""
        UPDATE facturas
        SET estado = CASE
            WHEN fecha_vencimiento < CURDATE() THEN 'vencida'
            ELSE 'pendiente'
        END
        WHERE estado = 'promesa'
          AND saldo_pendiente > 0
    """))


def downgrade():
    bind = op.get_bind()

    for column in ["creada_manual", "afecta_corte", "descripcion", "concepto", "tipo_factura"]:
        if _has_column(bind, "facturas", column):
            op.drop_column("facturas", column)
