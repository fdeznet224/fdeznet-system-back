"""Cobros CoDi: texto del QR y cuándo vence (para el QR y la revisión de respaldo).

Revision ID: c9e5a7b3d4f6
Revises: b8d4f6a1c3e2
"""
import sqlalchemy as sa
from alembic import op

revision = "c9e5a7b3d4f6"
down_revision = "b8d4f6a1c3e2"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("cobros_codi") as tabla:
        tabla.add_column(sa.Column("qr_texto", sa.Text(), nullable=True))
        tabla.add_column(sa.Column("vence_en", sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table("cobros_codi") as tabla:
        tabla.drop_column("vence_en")
        tabla.drop_column("qr_texto")
