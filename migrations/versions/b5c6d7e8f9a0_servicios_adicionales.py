"""agrega servicios adicionales recurrentes

Revision ID: b5c6d7e8f9a0
Revises: a4b5c6d7e8f9
Create Date: 2026-09-07
"""

from alembic import op
import sqlalchemy as sa


revision = "b5c6d7e8f9a0"
down_revision = "a4b5c6d7e8f9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "servicios_adicionales",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("cliente_id", sa.Integer(), nullable=False),
        sa.Column("servicio_id", sa.Integer(), nullable=True),
        sa.Column("nombre", sa.String(150), nullable=False),
        sa.Column("precio_mensual", sa.Numeric(12, 2), nullable=False),
        sa.Column("afecta_corte", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("activo", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("fecha_inicio", sa.Date(), nullable=False, server_default=sa.text("CURRENT_DATE")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["cliente_id"], ["clientes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["servicio_id"], ["servicios.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_servicios_adicionales_cliente_id", "servicios_adicionales", ["cliente_id"])
    op.create_index("ix_servicios_adicionales_servicio_id", "servicios_adicionales", ["servicio_id"])


def downgrade() -> None:
    op.drop_table("servicios_adicionales")
