"""Celular con el que el cliente paga por CoDi (el de la app de su banco).

Revision ID: a7c3e5f9b2d1
Revises: f2a9c4e1b7d3
"""
import sqlalchemy as sa
from alembic import op

revision = "a7c3e5f9b2d1"
down_revision = "f2a9c4e1b7d3"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("clientes") as tabla:
        tabla.add_column(sa.Column("telefono_codi", sa.String(10), nullable=True))


def downgrade():
    with op.batch_alter_table("clientes") as tabla:
        tabla.drop_column("telefono_codi")
