"""Redes con tipo (clientes o antenas) y rango opcional: pool de IP de antenas.

Revision ID: d7f2b4c8e1a9
Revises: c6e1a3b9d5f7
"""
import sqlalchemy as sa
from alembic import op

revision = "d7f2b4c8e1a9"
down_revision = "c6e1a3b9d5f7"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("redes") as tabla:
        tabla.add_column(sa.Column("tipo", sa.String(20), nullable=False, server_default="clientes"))
        tabla.add_column(sa.Column("ip_inicio", sa.String(45), nullable=True))
        tabla.add_column(sa.Column("ip_fin", sa.String(45), nullable=True))


def downgrade():
    with op.batch_alter_table("redes") as tabla:
        tabla.drop_column("ip_fin")
        tabla.drop_column("ip_inicio")
        tabla.drop_column("tipo")
