"""huella única de la captura del comprobante

Revision ID: 8e7d6c5b4a39
Revises: 9f8e7d6c5b4a
Create Date: 2026-09-30
"""

from alembic import op
import sqlalchemy as sa


revision = "8e7d6c5b4a39"
down_revision = "9f8e7d6c5b4a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("comprobantes_pago_revision") as tabla:
        tabla.add_column(sa.Column("huella_captura", sa.String(length=24), nullable=True))
        tabla.create_index("ix_comprobantes_pago_revision_huella_captura", ["huella_captura"])


def downgrade() -> None:
    with op.batch_alter_table("comprobantes_pago_revision") as tabla:
        tabla.drop_index("ix_comprobantes_pago_revision_huella_captura")
        tabla.drop_column("huella_captura")
