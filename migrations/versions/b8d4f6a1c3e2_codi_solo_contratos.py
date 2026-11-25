"""CoDi en prueba: solo para los contratos indicados.

Revision ID: b8d4f6a1c3e2
Revises: a7c3e5f9b2d1
"""
import sqlalchemy as sa
from alembic import op

revision = "b8d4f6a1c3e2"
down_revision = "a7c3e5f9b2d1"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("configuracion_correo_banco") as tabla:
        tabla.add_column(sa.Column("codi_solo_contratos", sa.String(255), nullable=True))


def downgrade():
    with op.batch_alter_table("configuracion_correo_banco") as tabla:
        tabla.drop_column("codi_solo_contratos")
