"""validacion de pagos por correo bancario

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-09-10 04:10:00
"""

from alembic import op
import sqlalchemy as sa


revision = "c2d3e4f5a6b7"
down_revision = "b1c2d3e4f5a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "configuracion_correo_banco",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("activo", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("auto_aprobar", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("proveedor", sa.String(30), nullable=False, server_default="gmail_imap"),
        sa.Column("correo", sa.String(160), nullable=True),
        sa.Column("secreto_cifrado", sa.Text(), nullable=True),
        sa.Column("remitente_permitido", sa.String(255), nullable=True),
        sa.Column("asunto_filtro", sa.String(255), nullable=True),
        sa.Column("carpeta", sa.String(100), nullable=False, server_default="INBOX"),
        sa.Column("ventana_dias", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("tolerancia_monto", sa.Numeric(12, 2), nullable=False, server_default="0.00"),
        sa.Column("requiere_dkim", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("ultimo_uid", sa.String(50), nullable=True),
        sa.Column("credencial_verificada_en", sa.DateTime(), nullable=True),
        sa.Column("ultima_revision", sa.DateTime(), nullable=True),
        sa.Column("ultimo_error", sa.String(500), nullable=True),
        sa.Column("actualizado_en", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_table(
        "transacciones_correo_banco",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("correo_message_id", sa.String(255), nullable=False),
        sa.Column("uid_buzon", sa.String(50), nullable=True),
        sa.Column("remitente", sa.String(255), nullable=False),
        sa.Column("asunto", sa.String(500), nullable=True),
        sa.Column("fecha_correo", sa.DateTime(), nullable=True),
        sa.Column("monto", sa.Numeric(12, 2), nullable=True),
        sa.Column("referencia", sa.String(100), nullable=True),
        sa.Column("concepto", sa.String(255), nullable=True),
        sa.Column("autenticado", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("detalle_autenticacion", sa.String(500), nullable=True),
        sa.Column("contenido_hash", sa.String(64), nullable=False),
        sa.Column("estado", sa.String(30), nullable=False, server_default="disponible"),
        sa.Column("pago_id", sa.Integer(), nullable=True),
        sa.Column("recibida_en", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("conciliada_en", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["pago_id"], ["pagos.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("correo_message_id", name="uq_transaccion_correo_message_id"),
        sa.UniqueConstraint("pago_id", name="uq_transaccion_correo_pago_id"),
    )
    op.create_index("ix_transacciones_correo_message_id", "transacciones_correo_banco", ["correo_message_id"])
    op.create_index("ix_transacciones_correo_fecha", "transacciones_correo_banco", ["fecha_correo"])
    op.create_index("ix_transacciones_correo_monto", "transacciones_correo_banco", ["monto"])
    op.create_index("ix_transacciones_correo_referencia", "transacciones_correo_banco", ["referencia"])
    op.add_column("comprobantes_pago_revision", sa.Column("transaccion_correo_id", sa.Integer(), nullable=True))
    op.create_unique_constraint(
        "uq_comprobante_transaccion_correo",
        "comprobantes_pago_revision",
        ["transaccion_correo_id"],
    )
    op.create_foreign_key(
        "fk_comprobante_transaccion_correo",
        "comprobantes_pago_revision",
        "transacciones_correo_banco",
        ["transaccion_correo_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_comprobante_transaccion_correo", "comprobantes_pago_revision", type_="foreignkey")
    op.drop_constraint("uq_comprobante_transaccion_correo", "comprobantes_pago_revision", type_="unique")
    op.drop_column("comprobantes_pago_revision", "transaccion_correo_id")
    op.drop_index("ix_transacciones_correo_referencia", table_name="transacciones_correo_banco")
    op.drop_index("ix_transacciones_correo_monto", table_name="transacciones_correo_banco")
    op.drop_index("ix_transacciones_correo_fecha", table_name="transacciones_correo_banco")
    op.drop_index("ix_transacciones_correo_message_id", table_name="transacciones_correo_banco")
    op.drop_table("transacciones_correo_banco")
    op.drop_table("configuracion_correo_banco")
