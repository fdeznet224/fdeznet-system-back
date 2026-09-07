"""bandeja de comprobantes de pago pendientes de revision

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
Create Date: 2026-09-06
"""

from alembic import op
import sqlalchemy as sa


revision = "e7f8a9b0c1d2"
down_revision = "d6e7f8a9b0c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "comprobantes_pago_revision",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("cliente_id", sa.Integer(), nullable=True),
        sa.Column("factura_id", sa.Integer(), nullable=True),
        sa.Column("pago_id", sa.Integer(), nullable=True),
        sa.Column("mensaje_chat_id", sa.Integer(), nullable=True),
        sa.Column("revisado_por_id", sa.Integer(), nullable=True),
        sa.Column("telefono", sa.String(100), nullable=False),
        sa.Column("media_url", sa.Text(), nullable=False),
        sa.Column("monto_detectado", sa.Numeric(12, 2), nullable=True),
        sa.Column("folio_detectado", sa.String(100), nullable=True),
        sa.Column("cedula_detectada", sa.String(20), nullable=True),
        sa.Column(
            "estado",
            sa.String(30),
            nullable=False,
            server_default="pendiente",
        ),
        sa.Column("motivo_revision", sa.String(100), nullable=False),
        sa.Column("notas_revision", sa.Text(), nullable=True),
        sa.Column(
            "fecha_recepcion",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("fecha_revision", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["cliente_id"], ["clientes.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["factura_id"], ["facturas.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["pago_id"], ["pagos.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["mensaje_chat_id"], ["mensajes_chat.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["revisado_por_id"], ["usuarios.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("pago_id"),
        sa.UniqueConstraint("mensaje_chat_id"),
    )
    op.create_index(
        "ix_comprobantes_revision_estado_fecha",
        "comprobantes_pago_revision",
        ["estado", "fecha_recepcion"],
    )
    op.create_index(
        "ix_comprobantes_pago_revision_folio_detectado",
        "comprobantes_pago_revision",
        ["folio_detectado"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_comprobantes_pago_revision_folio_detectado",
        table_name="comprobantes_pago_revision",
    )
    op.drop_index(
        "ix_comprobantes_revision_estado_fecha",
        table_name="comprobantes_pago_revision",
    )
    op.drop_table("comprobantes_pago_revision")
