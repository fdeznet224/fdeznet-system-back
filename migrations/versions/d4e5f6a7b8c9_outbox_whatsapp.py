"""agrega bandeja persistente y reintentos de WhatsApp

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-07-29
"""

from alembic import op
import sqlalchemy as sa


revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade():
    columnas = (
        sa.Column(
            "estado_envio",
            sa.String(20),
            nullable=False,
            server_default="pendiente",
        ),
        sa.Column(
            "intentos",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "max_intentos",
            sa.Integer(),
            nullable=False,
            server_default="5",
        ),
        sa.Column("ultimo_error", sa.Text(), nullable=True),
        sa.Column(
            "ultima_tentativa_en",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "proximo_intento_en",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "bloqueado_hasta",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "enviado_en",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "entregado_en",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "leido_en",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("ruta_archivo", sa.String(500), nullable=True),
        sa.Column("lote_id", sa.String(36), nullable=True),
        sa.Column(
            "reintentos_manuales",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("creado_por_id", sa.Integer(), nullable=True),
        sa.Column(
            "ultimo_reintento_por_id",
            sa.Integer(),
            nullable=True,
        ),
    )
    for columna in columnas:
        op.add_column("mensajes_chat", columna)

    op.create_foreign_key(
        "fk_mensajes_chat_creado_por",
        "mensajes_chat",
        "usuarios",
        ["creado_por_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_mensajes_chat_ultimo_reintento",
        "mensajes_chat",
        "usuarios",
        ["ultimo_reintento_por_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_mensajes_chat_lote_id",
        "mensajes_chat",
        ["lote_id"],
    )
    op.create_index(
        "ix_mensajes_chat_creado_por_id",
        "mensajes_chat",
        ["creado_por_id"],
    )
    op.create_index(
        "ix_mensajes_salida_estado_proximo",
        "mensajes_chat",
        ["direccion", "estado_envio", "proximo_intento_en"],
    )
    op.create_index(
        "ix_mensajes_salida_fecha",
        "mensajes_chat",
        ["direccion", "fecha"],
    )

    op.execute(
        """
        UPDATE mensajes_chat
        SET estado_envio = CASE
            WHEN direccion = 'entrada' THEN 'recibido'
            WHEN ack < 0 THEN 'fallido'
            WHEN ack = 0 THEN 'pendiente'
            WHEN ack = 1 THEN 'enviado'
            WHEN ack = 2 THEN 'entregado'
            ELSE 'leido'
        END
        """
    )


def downgrade():
    op.drop_index(
        "ix_mensajes_salida_fecha",
        table_name="mensajes_chat",
    )
    op.drop_index(
        "ix_mensajes_salida_estado_proximo",
        table_name="mensajes_chat",
    )
    op.drop_index(
        "ix_mensajes_chat_creado_por_id",
        table_name="mensajes_chat",
    )
    op.drop_index(
        "ix_mensajes_chat_lote_id",
        table_name="mensajes_chat",
    )
    op.drop_constraint(
        "fk_mensajes_chat_ultimo_reintento",
        "mensajes_chat",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_mensajes_chat_creado_por",
        "mensajes_chat",
        type_="foreignkey",
    )
    for columna in (
        "ultimo_reintento_por_id",
        "creado_por_id",
        "reintentos_manuales",
        "lote_id",
        "ruta_archivo",
        "leido_en",
        "entregado_en",
        "enviado_en",
        "bloqueado_hasta",
        "proximo_intento_en",
        "ultima_tentativa_en",
        "ultimo_error",
        "max_intentos",
        "intentos",
        "estado_envio",
    ):
        op.drop_column("mensajes_chat", columna)
