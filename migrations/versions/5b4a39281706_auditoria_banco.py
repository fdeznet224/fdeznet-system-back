"""auditoría con el correo del banco de los pagos aprobados por captura

Revision ID: 5b4a39281706
Revises: 6c5b4a392817
Create Date: 2026-09-30
"""

from alembic import op
import sqlalchemy as sa


revision = "5b4a39281706"
down_revision = "6c5b4a392817"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("comprobantes_pago_revision") as tabla:
        tabla.add_column(sa.Column("auditoria_banco", sa.String(length=20), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("comprobantes_pago_revision") as tabla:
        tabla.drop_column("auditoria_banco")
