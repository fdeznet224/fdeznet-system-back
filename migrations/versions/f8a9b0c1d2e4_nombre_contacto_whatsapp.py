"""nombre con el que se presenta cada chat de WhatsApp

Revision ID: f8a9b0c1d2e4
Revises: e7f8a9b0c1d3
Create Date: 2026-09-30
"""

from alembic import op
import sqlalchemy as sa


revision = "f8a9b0c1d2e4"
down_revision = "e7f8a9b0c1d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("whatsapp_identidades") as tabla:
        tabla.add_column(sa.Column("nombre_contacto", sa.String(150), nullable=True))
        tabla.add_column(sa.Column("nombre_registrado_en", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("whatsapp_identidades") as tabla:
        tabla.drop_column("nombre_registrado_en")
        tabla.drop_column("nombre_contacto")
