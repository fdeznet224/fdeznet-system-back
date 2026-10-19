"""Un chat de WhatsApp puede atender varios contratos

Quien paga su casa y el local (o la casa de un familiar) escribe desde un solo
chat. Cada contrato con el que se identifica queda ligado al chat; el que está
en uso sigue en whatsapp_identidades. Se llena con los chats ya identificados.

Revision ID: 9e4f5a6b7c8d
Revises: 8d3e4f5a6b7c
Create Date: 2026-10-19
"""

from alembic import op
import sqlalchemy as sa


revision = "9e4f5a6b7c8d"
down_revision = "8d3e4f5a6b7c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "whatsapp_contratos",
        sa.Column("telefono", sa.String(60), primary_key=True),
        sa.Column(
            "cliente_id",
            sa.Integer,
            sa.ForeignKey("clientes.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("etiqueta", sa.String(60), nullable=True),
        sa.Column("creado_en", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.execute(
        """
        INSERT INTO whatsapp_contratos (telefono, cliente_id, creado_en)
        SELECT telefono, cliente_id, COALESCE(verificado_en, NOW())
        FROM whatsapp_identidades
        WHERE cliente_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_table("whatsapp_contratos")
