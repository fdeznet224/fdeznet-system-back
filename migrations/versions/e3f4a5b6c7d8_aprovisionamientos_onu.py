"""Configuración automática de la ONU de los clientes nuevos

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-10-23
"""

from alembic import op
import sqlalchemy as sa


revision = "e3f4a5b6c7d8"
down_revision = "d2e3f4a5b6c7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "aprovisionamientos_onu",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("cliente_id", sa.Integer, sa.ForeignKey("clientes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("identificador", sa.String(100), nullable=False),
        sa.Column("ssid", sa.String(32), nullable=False),
        sa.Column("clave_wifi", sa.String(63), nullable=False),
        sa.Column("estado", sa.String(20), nullable=False, server_default="pendiente"),
        sa.Column("intentos", sa.Integer, nullable=False, server_default="0"),
        sa.Column("detalle", sa.String(255), nullable=True),
        sa.Column("device_id", sa.String(120), nullable=True),
        sa.Column("creado_en", sa.DateTime, nullable=False),
        sa.Column("aplicado_en", sa.DateTime, nullable=True),
    )
    op.create_index("ix_aprovisionamientos_onu_cliente_id", "aprovisionamientos_onu", ["cliente_id"])
    op.create_index("ix_aprovisionamientos_onu_estado", "aprovisionamientos_onu", ["estado"])


def downgrade() -> None:
    op.drop_table("aprovisionamientos_onu")
