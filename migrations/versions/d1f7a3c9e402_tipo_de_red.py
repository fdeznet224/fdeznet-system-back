"""Tipo de red del ISP (WISP, fibra o híbrido)

configuracion_sistema.tipo_red: lo elige el admin la primera vez que entra
y el menú se adapta (un WISP por radioenlaces no ve OLT, TR-069 ni NAP).
Vacío = todavía no lo eligió.

Revision ID: d1f7a3c9e402
Revises: c9e2a4b6d801
Create Date: 2026-11-08
"""

import sqlalchemy as sa
from alembic import op

revision = "d1f7a3c9e402"
down_revision = "c9e2a4b6d801"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("configuracion_sistema") as tabla:
        tabla.add_column(sa.Column("tipo_red", sa.String(10), nullable=True))


def downgrade():
    with op.batch_alter_table("configuracion_sistema") as tabla:
        tabla.drop_column("tipo_red")
