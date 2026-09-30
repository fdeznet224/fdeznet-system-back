"""concepto y cuentas leídos en la captura del comprobante

Revision ID: 7d6c5b4a3928
Revises: 8e7d6c5b4a39
Create Date: 2026-09-30
"""

from alembic import op
import sqlalchemy as sa


revision = "7d6c5b4a3928"
down_revision = "8e7d6c5b4a39"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("comprobantes_pago_revision") as tabla:
        tabla.add_column(sa.Column("concepto_detectado", sa.String(length=120), nullable=True))
        tabla.add_column(sa.Column("cuentas_detectadas", sa.String(length=60), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("comprobantes_pago_revision") as tabla:
        tabla.drop_column("cuentas_detectadas")
        tabla.drop_column("concepto_detectado")
