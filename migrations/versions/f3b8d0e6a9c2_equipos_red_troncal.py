"""Red troncal: equipos arriba de los MikroTik y el MikroTik que cuelga de cada uno.

Revision ID: f3b8d0e6a9c2
Revises: e2a7c9d5f8b1
"""
import sqlalchemy as sa
from alembic import op

revision = "f3b8d0e6a9c2"
down_revision = "e2a7c9d5f8b1"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("equipos_red") as tabla:
        tabla.add_column(sa.Column("troncal", sa.Boolean(), nullable=False, server_default="0"))
        tabla.add_column(sa.Column("router_hijo_id", sa.Integer(), nullable=True))
        tabla.create_foreign_key("fk_equipos_red_router_hijo", "routers", ["router_hijo_id"], ["id"], ondelete="SET NULL")


def downgrade():
    with op.batch_alter_table("equipos_red") as tabla:
        tabla.drop_constraint("fk_equipos_red_router_hijo", type_="foreignkey")
        tabla.drop_column("router_hijo_id")
        tabla.drop_column("troncal")
