"""agrega configuracion de marca blanca

Revision ID: d7e8f9a0b1c2
Revises: c6d7e8f9a0b1
Create Date: 2026-09-08
"""

from alembic import op
import sqlalchemy as sa


revision = "d7e8f9a0b1c2"
down_revision = "c6d7e8f9a0b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("configuracion_sistema", sa.Column("empresa_nombre", sa.String(120), nullable=False, server_default="FdezNet"))
    op.add_column("configuracion_sistema", sa.Column("sistema_nombre", sa.String(120), nullable=False, server_default="FdezPay"))
    op.add_column("configuracion_sistema", sa.Column("logo_url", sa.String(500), nullable=True))
    op.add_column("configuracion_sistema", sa.Column("favicon_url", sa.String(500), nullable=True))
    op.add_column("configuracion_sistema", sa.Column("color_primario", sa.String(7), nullable=False, server_default="#2563eb"))
    op.add_column("configuracion_sistema", sa.Column("color_secundario", sa.String(7), nullable=False, server_default="#4f46e5"))
    op.add_column("configuracion_sistema", sa.Column("empresa_telefono", sa.String(40), nullable=True))
    op.add_column("configuracion_sistema", sa.Column("empresa_email", sa.String(160), nullable=True))
    op.add_column("configuracion_sistema", sa.Column("empresa_direccion", sa.String(300), nullable=True))
    op.add_column("configuracion_sistema", sa.Column("pie_recibo", sa.String(300), nullable=True))


def downgrade() -> None:
    for column in reversed([
        "empresa_nombre", "sistema_nombre", "logo_url", "favicon_url",
        "color_primario", "color_secundario", "empresa_telefono",
        "empresa_email", "empresa_direccion", "pie_recibo",
    ]):
        op.drop_column("configuracion_sistema", column)
