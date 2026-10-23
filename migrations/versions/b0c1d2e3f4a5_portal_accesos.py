"""Contraseña propia del cliente en su portal

Sin fila, el cliente entra con su contrato y el portal le pide cambiarla.

Revision ID: b0c1d2e3f4a5
Revises: af5a6b7c8d9e
Create Date: 2026-10-23
"""

from alembic import op
import sqlalchemy as sa


revision = "b0c1d2e3f4a5"
down_revision = "af5a6b7c8d9e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "portal_accesos",
        sa.Column("cliente_id", sa.Integer, sa.ForeignKey("clientes.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("cambiado_en", sa.DateTime, nullable=False),
        sa.Column("ultimo_acceso", sa.DateTime, nullable=True),
    )


def downgrade() -> None:
    op.drop_table("portal_accesos")
