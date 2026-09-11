"""validar cuenta destino y sentido del movimiento bancario

Revision ID: c3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-09-10 17:20:00
"""

from alembic import op
import sqlalchemy as sa


revision = "c3e4f5a6b7c8"
down_revision = "c2d3e4f5a6b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "configuracion_correo_banco",
        sa.Column("cuentas_destino_permitidas", sa.String(255), nullable=True),
    )
    op.add_column(
        "transacciones_correo_banco",
        sa.Column("tipo_movimiento", sa.String(20), nullable=True),
    )
    op.add_column(
        "transacciones_correo_banco",
        sa.Column("cuenta_destino_terminacion", sa.String(4), nullable=True),
    )
    op.create_index(
        "ix_transacciones_correo_cuenta_destino",
        "transacciones_correo_banco",
        ["cuenta_destino_terminacion"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_transacciones_correo_cuenta_destino",
        table_name="transacciones_correo_banco",
    )
    op.drop_column("transacciones_correo_banco", "cuenta_destino_terminacion")
    op.drop_column("transacciones_correo_banco", "tipo_movimiento")
    op.drop_column("configuracion_correo_banco", "cuentas_destino_permitidas")
