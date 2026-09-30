"""agente de IA para WhatsApp: configuración, interacciones e identidades

Revision ID: e7f8a9b0c1d3
Revises: d5e6f7a8b9c1
Create Date: 2026-09-30
"""

from alembic import op
import sqlalchemy as sa


revision = "e7f8a9b0c1d3"
down_revision = "d5e6f7a8b9c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("configuracion_bot") as tabla:
        tabla.add_column(sa.Column("agente_modo", sa.String(20), nullable=False, server_default="apagado"))
        tabla.add_column(sa.Column("agente_url", sa.String(255), nullable=False, server_default="https://api.deepseek.com/v1"))
        tabla.add_column(sa.Column("agente_modelo", sa.String(80), nullable=False, server_default="deepseek-flash"))
        tabla.add_column(sa.Column("agente_api_key_cifrada", sa.Text(), nullable=True))
        tabla.add_column(sa.Column("agente_conocimiento", sa.Text(), nullable=True))

    op.create_table(
        "agente_interacciones",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("telefono", sa.String(60), nullable=False),
        sa.Column("cliente_id", sa.Integer(), sa.ForeignKey("clientes.id", ondelete="SET NULL"), nullable=True),
        sa.Column("mensaje_chat_id", sa.Integer(), sa.ForeignKey("mensajes_chat.id", ondelete="SET NULL"), nullable=True),
        sa.Column("modo", sa.String(20), nullable=False),
        sa.Column("estado", sa.String(20), nullable=False, server_default="pendiente"),
        sa.Column("mensaje_cliente", sa.Text(), nullable=True),
        sa.Column("respuesta_propuesta", sa.Text(), nullable=True),
        sa.Column("respuesta_enviada", sa.Text(), nullable=True),
        sa.Column("herramientas_json", sa.Text(), nullable=True),
        sa.Column("acciones_pendientes_json", sa.Text(), nullable=True),
        sa.Column("acciones_resultado_json", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("tokens_entrada", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_salida", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("costo_usd", sa.Numeric(10, 6), nullable=False, server_default="0"),
        sa.Column("revisado_por_id", sa.Integer(), sa.ForeignKey("usuarios.id", ondelete="SET NULL"), nullable=True),
        sa.Column("creado_en", sa.DateTime(), nullable=False),
        sa.Column("revisado_en", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_agente_interacciones_telefono", "agente_interacciones", ["telefono"])
    op.create_index("ix_agente_interacciones_cliente_id", "agente_interacciones", ["cliente_id"])
    op.create_index("ix_agente_interacciones_estado", "agente_interacciones", ["estado"])
    op.create_index("ix_agente_interacciones_creado_en", "agente_interacciones", ["creado_en"])

    op.create_table(
        "whatsapp_identidades",
        sa.Column("telefono", sa.String(60), primary_key=True),
        sa.Column("cliente_id", sa.Integer(), sa.ForeignKey("clientes.id", ondelete="CASCADE"), nullable=True),
        sa.Column("verificado_en", sa.DateTime(), nullable=True),
        sa.Column("intentos_fallidos", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ultimo_intento_en", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("whatsapp_identidades")
    op.drop_index("ix_agente_interacciones_creado_en", table_name="agente_interacciones")
    op.drop_index("ix_agente_interacciones_estado", table_name="agente_interacciones")
    op.drop_index("ix_agente_interacciones_cliente_id", table_name="agente_interacciones")
    op.drop_index("ix_agente_interacciones_telefono", table_name="agente_interacciones")
    op.drop_table("agente_interacciones")
    with op.batch_alter_table("configuracion_bot") as tabla:
        for columna in (
            "agente_conocimiento",
            "agente_api_key_cifrada",
            "agente_modelo",
            "agente_url",
            "agente_modo",
        ):
            tabla.drop_column(columna)
