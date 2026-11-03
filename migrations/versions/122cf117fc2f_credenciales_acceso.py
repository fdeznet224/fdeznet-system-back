"""Llaves de acceso (huella o cara del celular) de los usuarios del sistema

Revision ID: 122cf117fc2f
Revises: d4e8f0a1b2c3
Create Date: 2026-11-03
"""

from alembic import op
import sqlalchemy as sa


revision = "122cf117fc2f"
down_revision = "d4e8f0a1b2c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "credenciales_acceso",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("usuario_id", sa.Integer, sa.ForeignKey("usuarios.id", ondelete="CASCADE"), nullable=False),
        sa.Column("credencial_id", sa.String(255), nullable=False, unique=True),
        sa.Column("llave_publica", sa.LargeBinary, nullable=False),
        sa.Column("contador", sa.Integer, nullable=False, server_default="0"),
        sa.Column("nombre", sa.String(80), nullable=True),
        sa.Column("creado", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("ultimo_uso", sa.DateTime, nullable=True),
    )
    op.create_index("ix_credenciales_acceso_usuario_id", "credenciales_acceso", ["usuario_id"])


def downgrade() -> None:
    op.drop_index("ix_credenciales_acceso_usuario_id", table_name="credenciales_acceso")
    op.drop_table("credenciales_acceso")
