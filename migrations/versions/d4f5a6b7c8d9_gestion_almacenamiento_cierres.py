"""gestion de almacenamiento y cierres mensuales

Revision ID: d4f5a6b7c8d9
Revises: c3e4f5a6b7c8
"""

from alembic import op
import sqlalchemy as sa


revision = "d4f5a6b7c8d9"
down_revision = "c3e4f5a6b7c8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("configuracion_sistema", sa.Column("limpieza_almacenamiento_automatica", sa.Boolean(), nullable=False, server_default="1"))
    op.add_column("configuracion_sistema", sa.Column("hora_limpieza_almacenamiento", sa.String(5), nullable=False, server_default="02:30"))
    op.add_column("configuracion_sistema", sa.Column("dias_retencion_comprobantes_rechazados", sa.Integer(), nullable=False, server_default="90"))
    op.add_column("configuracion_sistema", sa.Column("dias_retencion_comprobantes_aprobados", sa.Integer(), nullable=False, server_default="365"))
    op.add_column("configuracion_sistema", sa.Column("dias_retencion_recibos_pdf", sa.Integer(), nullable=False, server_default="180"))
    op.add_column("configuracion_sistema", sa.Column("dias_retencion_archivos_whatsapp", sa.Integer(), nullable=False, server_default="90"))
    op.add_column("configuracion_sistema", sa.Column("dias_retencion_respaldos", sa.Integer(), nullable=False, server_default="14"))
    op.add_column("configuracion_sistema", sa.Column("cierre_mensual_automatico", sa.Boolean(), nullable=False, server_default="1"))
    op.add_column("configuracion_sistema", sa.Column("dia_cierre_almacenamiento", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("configuracion_sistema", sa.Column("ultima_limpieza_almacenamiento", sa.DateTime(), nullable=True))
    op.add_column("configuracion_sistema", sa.Column("ultimo_cierre_almacenamiento", sa.String(7), nullable=True))

    op.add_column("comprobantes_pago_revision", sa.Column("archivado_en", sa.DateTime(), nullable=True))
    op.add_column("comprobantes_pago_revision", sa.Column("archivo_eliminado_en", sa.DateTime(), nullable=True))

    op.create_table(
        "cierres_almacenamiento",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("periodo", sa.String(7), nullable=False),
        sa.Column("tipo", sa.String(20), nullable=False, server_default="manual"),
        sa.Column("comprobantes_aprobados", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("comprobantes_rechazados", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("comprobantes_pendientes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bytes_comprobantes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("bytes_liberados", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cerrado_por_id", sa.Integer(), sa.ForeignKey("usuarios.id", ondelete="SET NULL"), nullable=True),
        sa.Column("cerrado_en", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("periodo", name="uq_cierres_almacenamiento_periodo"),
    )


def downgrade() -> None:
    op.drop_table("cierres_almacenamiento")
    op.drop_column("comprobantes_pago_revision", "archivo_eliminado_en")
    op.drop_column("comprobantes_pago_revision", "archivado_en")
    for column in (
        "ultimo_cierre_almacenamiento",
        "ultima_limpieza_almacenamiento",
        "dia_cierre_almacenamiento",
        "cierre_mensual_automatico",
        "dias_retencion_respaldos",
        "dias_retencion_archivos_whatsapp",
        "dias_retencion_recibos_pdf",
        "dias_retencion_comprobantes_aprobados",
        "dias_retencion_comprobantes_rechazados",
        "hora_limpieza_almacenamiento",
        "limpieza_almacenamiento_automatica",
    ):
        op.drop_column("configuracion_sistema", column)
