"""Línea de internet (principal/respaldo) de cada router.

Revision ID: e4b8c2d6f913
Revises: d1f7a3c9e402
"""
import sqlalchemy as sa
from alembic import op

revision = "e4b8c2d6f913"
down_revision = "d1f7a3c9e402"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("routers") as tabla:
        tabla.add_column(sa.Column("wan_estado", sa.String(15), nullable=True))
        tabla.add_column(sa.Column("wan_enlace", sa.String(150), nullable=True))
        tabla.add_column(sa.Column("wan_principal", sa.String(150), nullable=True))
        tabla.add_column(sa.Column("wan_desde", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    with op.batch_alter_table("routers") as tabla:
        tabla.drop_column("wan_desde")
        tabla.drop_column("wan_principal")
        tabla.drop_column("wan_enlace")
        tabla.drop_column("wan_estado")
