"""Ping de la red cada minuto: fallas seguidas, desde cuándo está caído y si ya se avisó.

Revision ID: c6e1a3b9d5f7
Revises: b5d0f2a8c4e6
"""
import sqlalchemy as sa
from alembic import op

revision = "c6e1a3b9d5f7"
down_revision = "b5d0f2a8c4e6"
branch_labels = None
depends_on = None


def upgrade():
    for tabla_nombre in ("equipos_red", "olts"):
        with op.batch_alter_table(tabla_nombre) as tabla:
            tabla.add_column(sa.Column("fallas_seguidas", sa.Integer(), nullable=False, server_default="0"))
            tabla.add_column(sa.Column("caido_desde", sa.DateTime(), nullable=True))
            tabla.add_column(sa.Column("alerta_enviada", sa.Boolean(), nullable=False, server_default="0"))


def downgrade():
    for tabla_nombre in ("equipos_red", "olts"):
        with op.batch_alter_table(tabla_nombre) as tabla:
            tabla.drop_column("alerta_enviada")
            tabla.drop_column("caido_desde")
            tabla.drop_column("fallas_seguidas")
