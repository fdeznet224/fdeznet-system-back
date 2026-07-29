"""agregar auditoria de actividad

Revision ID: e8a9b0c1d2e3
Revises: d7f8a9b0c1d2
Create Date: 2026-07-28
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "e8a9b0c1d2e3"
down_revision = "d7f8a9b0c1d2"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if "logs_actividad" in inspect(bind).get_table_names():
        return

    op.create_table(
        "logs_actividad",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("usuario_id", sa.Integer(), nullable=True),
        sa.Column("usuario_nombre", sa.String(length=50), nullable=True),
        sa.Column("accion", sa.String(length=100), nullable=False),
        sa.Column("metodo", sa.String(length=10), nullable=False),
        sa.Column("ruta", sa.String(length=255), nullable=False),
        sa.Column("estado_http", sa.Integer(), nullable=False),
        sa.Column("detalle", sa.Text(), nullable=True),
        sa.Column("ip_cliente", sa.String(length=45), nullable=True),
        sa.Column(
            "fecha",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["usuario_id"],
            ["usuarios.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_logs_actividad_fecha", "logs_actividad", ["fecha"])
    op.create_index(
        "ix_logs_actividad_usuario_fecha",
        "logs_actividad",
        ["usuario_id", "fecha"],
    )
    op.create_index("ix_logs_actividad_accion", "logs_actividad", ["accion"])


def downgrade():
    bind = op.get_bind()
    if "logs_actividad" in inspect(bind).get_table_names():
        op.drop_table("logs_actividad")
