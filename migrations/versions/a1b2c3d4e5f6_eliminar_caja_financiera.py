"""elimina el módulo de caja financiera; la liquidación es por pagos y período"""

from alembic import op
import sqlalchemy as sa


revision = "a1b2c3d4e5f6"
down_revision = ("f9b0c1d2e3f4", "e4f5a6b7c8d9")
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tablas = set(inspector.get_table_names())

    if "pagos" in tablas:
        columnas = {c["name"] for c in inspector.get_columns("pagos")}
        if "caja_sesion_id" in columnas:
            foreign_keys = {
                fk["name"] for fk in inspector.get_foreign_keys("pagos")
            }
            if "fk_pago_caja" in foreign_keys:
                op.drop_constraint("fk_pago_caja", "pagos", type_="foreignkey")
            indices = {idx["name"] for idx in inspector.get_indexes("pagos")}
            if "ix_pagos_caja_sesion_id" in indices:
                op.drop_index("ix_pagos_caja_sesion_id", table_name="pagos")
            op.drop_column("pagos", "caja_sesion_id")
    if "bajas_servicio" in tablas:
        columnas_baja = {c["name"] for c in inspector.get_columns("bajas_servicio")}
        if "deuda_al_baja" in columnas_baja:
            op.drop_column("bajas_servicio", "deuda_al_baja")
    # movimientos referencia a pagos y cajas_sesion; se elimina primero.
    if "movimientos_caja" in tablas:
        op.drop_table("movimientos_caja")
    if "cajas_sesion" in tablas:
        op.drop_table("cajas_sesion")


def downgrade():
    op.create_table(
        "cajas_sesion",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("usuario_id", sa.Integer(), nullable=False),
        sa.Column("estado", sa.String(20), nullable=False, server_default="abierta"),
        sa.Column("monto_apertura", sa.Numeric(12, 2), nullable=False, server_default="0.00"),
    )
    op.add_column(
        "pagos",
        sa.Column("caja_sesion_id", sa.Integer(), nullable=True),
    )
