"""Cobro CoDi con pey.mx: llaves y cobros enviados.

Revision ID: f2a9c4e1b7d3
Revises: e4b8c2d6f913
"""
import sqlalchemy as sa
from alembic import op

revision = "f2a9c4e1b7d3"
down_revision = "e4b8c2d6f913"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("configuracion_correo_banco") as tabla:
        tabla.add_column(sa.Column("codi_activo", sa.Boolean(), nullable=False, server_default="0"))
        tabla.add_column(sa.Column("pey_api_key", sa.Text(), nullable=True))
        tabla.add_column(sa.Column("pey_webhook_secreto", sa.Text(), nullable=True))
    op.create_table(
        "cobros_codi",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("cliente_id", sa.Integer(), sa.ForeignKey("clientes.id", ondelete="SET NULL"), nullable=True),
        sa.Column("tipo", sa.String(10), nullable=False, server_default="celular"),
        sa.Column("telefono", sa.String(10), nullable=True),
        sa.Column("monto", sa.Numeric(12, 2), nullable=False),
        sa.Column("estado", sa.String(20), nullable=False, server_default="enviado"),
        sa.Column("pey_transaction_id", sa.String(40), nullable=True, unique=True),
        sa.Column("codi_id", sa.String(40), nullable=True),
        sa.Column("num_ref", sa.String(7), nullable=True),
        sa.Column("monto_pagado", sa.Numeric(12, 2), nullable=True),
        sa.Column("pago_ids", sa.String(255), nullable=True),
        sa.Column("detalle", sa.String(500), nullable=True),
        sa.Column("origen", sa.String(20), nullable=False, server_default="whatsapp"),
        sa.Column("creado_en", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("pagado_en", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_cobros_codi_cliente_id", "cobros_codi", ["cliente_id"])
    op.create_index("ix_cobros_codi_estado", "cobros_codi", ["estado"])


def downgrade():
    op.drop_index("ix_cobros_codi_estado", table_name="cobros_codi")
    op.drop_index("ix_cobros_codi_cliente_id", table_name="cobros_codi")
    op.drop_table("cobros_codi")
    with op.batch_alter_table("configuracion_correo_banco") as tabla:
        tabla.drop_column("pey_webhook_secreto")
        tabla.drop_column("pey_api_key")
        tabla.drop_column("codi_activo")
