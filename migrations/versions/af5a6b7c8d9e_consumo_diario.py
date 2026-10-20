"""Consumo de datos por día de cada servicio

Cada 5 minutos se leen los contadores de las sesiones PPPoE del MikroTik y lo
que crecieron se suma al día. Sirve para mostrarle al cliente qué días usó
internet cuando dice que no tuvo servicio.

Revision ID: af5a6b7c8d9e
Revises: 9e4f5a6b7c8d
Create Date: 2026-10-19
"""

from alembic import op
import sqlalchemy as sa


revision = "af5a6b7c8d9e"
down_revision = "9e4f5a6b7c8d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "consumo_diario",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("servicio_id", sa.Integer, sa.ForeignKey("servicios.id", ondelete="CASCADE"), nullable=False),
        sa.Column("cliente_id", sa.Integer, sa.ForeignKey("clientes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("fecha", sa.Date, nullable=False),
        sa.Column("bajada_bytes", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("subida_bytes", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("minutos_en_linea", sa.Integer, nullable=False, server_default="0"),
        sa.Column("sesion_mk", sa.String(30), nullable=True),
        sa.Column("ultimo_rx", sa.BigInteger, nullable=True),
        sa.Column("ultimo_tx", sa.BigInteger, nullable=True),
        sa.Column("ultima_lectura", sa.DateTime, nullable=True),
        sa.UniqueConstraint("servicio_id", "fecha", name="uq_consumo_diario_servicio_fecha"),
    )
    op.create_index("ix_consumo_diario_cliente_fecha", "consumo_diario", ["cliente_id", "fecha"])
    op.create_index("ix_consumo_diario_fecha", "consumo_diario", ["fecha"])


def downgrade() -> None:
    op.drop_table("consumo_diario")
