"""operaciones idempotentes para sincronizacion movil

Revision ID: c2e3f4a5b6c7
Revises: b1d2e3f4a5b6
Create Date: 2026-07-28
"""
from alembic import op
import sqlalchemy as sa


revision = "c2e3f4a5b6c7"
down_revision = "b1d2e3f4a5b6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "operaciones_sincronizacion",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("usuario_id", sa.Integer(), nullable=False),
        sa.Column("tipo", sa.String(40), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("respuesta", sa.Text(), nullable=False),
        sa.Column("creado_cliente", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "aplicado_en",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["usuario_id"],
            ["usuarios.id"],
            name="fk_operacion_sincronizacion_usuario",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_operaciones_sincronizacion_usuario_id",
        "operaciones_sincronizacion",
        ["usuario_id"],
    )
    op.create_index(
        "ix_operaciones_sincronizacion_usuario_fecha",
        "operaciones_sincronizacion",
        ["usuario_id", "aplicado_en"],
    )


def downgrade():
    op.drop_index(
        "ix_operaciones_sincronizacion_usuario_fecha",
        table_name="operaciones_sincronizacion",
    )
    op.drop_index(
        "ix_operaciones_sincronizacion_usuario_id",
        table_name="operaciones_sincronizacion",
    )
    op.drop_table("operaciones_sincronizacion")
