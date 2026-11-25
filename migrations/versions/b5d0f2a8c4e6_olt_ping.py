"""OLT: último ping desde su MikroTik (estado en la topología).

Revision ID: b5d0f2a8c4e6
Revises: a4c9e1f7b3d5
"""
import sqlalchemy as sa
from alembic import op

revision = "b5d0f2a8c4e6"
down_revision = "a4c9e1f7b3d5"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("olts") as tabla:
        tabla.add_column(sa.Column("ping_responde", sa.Boolean(), nullable=True))
        tabla.add_column(sa.Column("ping_latencia_ms", sa.Float(), nullable=True))
        tabla.add_column(sa.Column("ping_revisado_en", sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table("olts") as tabla:
        tabla.drop_column("ping_revisado_en")
        tabla.drop_column("ping_latencia_ms")
        tabla.drop_column("ping_responde")
