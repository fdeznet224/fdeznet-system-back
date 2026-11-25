"""Topología: la OLT puede colgar de un equipo de red (p. ej. el PtP que le llega).

Revision ID: a4c9e1f7b3d5
Revises: f3b8d0e6a9c2
"""
import sqlalchemy as sa
from alembic import op

revision = "a4c9e1f7b3d5"
down_revision = "f3b8d0e6a9c2"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("olts") as tabla:
        tabla.add_column(sa.Column("equipo_padre_id", sa.Integer(), nullable=True))
        tabla.create_foreign_key("fk_olts_equipo_padre", "equipos_red", ["equipo_padre_id"], ["id"], ondelete="SET NULL")


def downgrade():
    with op.batch_alter_table("olts") as tabla:
        tabla.drop_constraint("fk_olts_equipo_padre", type_="foreignkey")
        tabla.drop_column("equipo_padre_id")
