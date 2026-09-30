"""hora de la transferencia leída en la captura del comprobante

Revision ID: 9f8e7d6c5b4a
Revises: f8a9b0c1d2e4
Create Date: 2026-09-30
"""

from alembic import op
import sqlalchemy as sa


revision = "9f8e7d6c5b4a"
down_revision = "f8a9b0c1d2e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("comprobantes_pago_revision") as tabla:
        tabla.add_column(sa.Column("fecha_pago_detectada", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("comprobantes_pago_revision") as tabla:
        tabla.drop_column("fecha_pago_detectada")
