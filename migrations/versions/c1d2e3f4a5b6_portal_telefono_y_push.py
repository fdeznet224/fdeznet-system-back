"""Portal del cliente: código para cambiar su teléfono y notificaciones push

Revision ID: c1d2e3f4a5b6
Revises: b0c1d2e3f4a5
Create Date: 2026-10-23
"""

from alembic import op
import sqlalchemy as sa


revision = "c1d2e3f4a5b6"
down_revision = "b0c1d2e3f4a5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("portal_accesos", sa.Column("telefono_pendiente", sa.String(20), nullable=True))
    op.add_column("portal_accesos", sa.Column("codigo_hash", sa.String(255), nullable=True))
    op.add_column("portal_accesos", sa.Column("codigo_expira", sa.DateTime, nullable=True))
    op.add_column("portal_accesos", sa.Column("codigo_intentos", sa.Integer, nullable=False, server_default="0"))
    op.create_table(
        "push_suscripciones",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("cliente_id", sa.Integer, sa.ForeignKey("clientes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("endpoint", sa.String(500), nullable=False),
        sa.Column("p256dh", sa.String(200), nullable=False),
        sa.Column("auth", sa.String(100), nullable=False),
        sa.Column("creado_en", sa.DateTime, nullable=False),
        sa.Column("ultimo_envio", sa.DateTime, nullable=True),
        sa.Column("fallos", sa.Integer, nullable=False, server_default="0"),
        sa.UniqueConstraint("endpoint", name="uq_push_suscripciones_endpoint"),
    )
    op.create_index("ix_push_suscripciones_cliente_id", "push_suscripciones", ["cliente_id"])


def downgrade() -> None:
    op.drop_table("push_suscripciones")
    for columna in ("codigo_intentos", "codigo_expira", "codigo_hash", "telefono_pendiente"):
        op.drop_column("portal_accesos", columna)
