"""Cobros CoDi: meses que adelanta el cliente con ese pago.

Revision ID: d1f6b8c4e5a7
Revises: c9e5a7b3d4f6
"""
import sqlalchemy as sa
from alembic import op

revision = "d1f6b8c4e5a7"
down_revision = "c9e5a7b3d4f6"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("cobros_codi") as tabla:
        tabla.add_column(sa.Column("meses_adelanto", sa.Integer(), nullable=False, server_default="0"))


def downgrade():
    with op.batch_alter_table("cobros_codi") as tabla:
        tabla.drop_column("meses_adelanto")
