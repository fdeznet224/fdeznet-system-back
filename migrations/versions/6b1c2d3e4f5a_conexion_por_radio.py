"""tipo de conexión del cliente (fibra o radioenlace) y su antena

Los clientes por radioenlace no tienen ONU: se revisa su antena (Ubiquiti)
con un ping desde su MikroTik. Se marcan como radio los que no tienen OLT y
están en una zona sin OLT (Sagrado Corazón); el resto queda como fibra.

Revision ID: 6b1c2d3e4f5a
Revises: 5a0b1c2d3e4f
Create Date: 2026-10-11
"""

from alembic import op
import sqlalchemy as sa


revision = "6b1c2d3e4f5a"
down_revision = "5a0b1c2d3e4f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("clientes", sa.Column("tipo_conexion", sa.String(20), nullable=False, server_default="fibra"))
    op.add_column("clientes", sa.Column("ip_antena", sa.String(45), nullable=True))
    op.add_column("clientes", sa.Column("antena_responde", sa.Boolean(), nullable=True))
    op.add_column("clientes", sa.Column("antena_latencia_ms", sa.Float(), nullable=True))
    op.add_column("clientes", sa.Column("antena_perdida", sa.Integer(), nullable=True))
    op.add_column("clientes", sa.Column("antena_revisada_en", sa.DateTime(), nullable=True))
    op.execute(
        """
        UPDATE clientes c
        JOIN zonas z ON z.id = c.zona_id
        SET c.tipo_conexion = 'radio'
        WHERE c.olt_id IS NULL AND z.olt_id IS NULL
        """
    )


def downgrade() -> None:
    for columna in ("antena_revisada_en", "antena_perdida", "antena_latencia_ms", "antena_responde", "ip_antena", "tipo_conexion"):
        op.drop_column("clientes", columna)
