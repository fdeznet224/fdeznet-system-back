"""agrega versiones, actualizaciones y respaldos

Revision ID: a0b1c2d3e4f5
Revises: f9a0b1c2d3e4
Create Date: 2026-09-08
"""

from alembic import op
import sqlalchemy as sa


revision = "a0b1c2d3e4f5"
down_revision = "f9a0b1c2d3e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("instalaciones_sistema", sa.Column("actualizacion_automatica", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("instalaciones_sistema", sa.Column("actualizacion_estado", sa.String(30), nullable=False, server_default="sin_actividad"))
    op.add_column("instalaciones_sistema", sa.Column("actualizacion_mensaje", sa.String(500), nullable=True))
    op.add_column("instalaciones_sistema", sa.Column("actualizacion_fecha", sa.DateTime(), nullable=True))
    op.add_column("instalaciones_sistema", sa.Column("ultimo_respaldo", sa.String(255), nullable=True))
    op.create_table(
        "versiones_sistema",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("version", sa.String(30), nullable=False),
        sa.Column("backend_commit", sa.String(40), nullable=False),
        sa.Column("frontend_commit", sa.String(40), nullable=False),
        sa.Column("notas", sa.Text(), nullable=True),
        sa.Column("activa", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("creada_en", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("version"),
    )
    op.create_index("ix_versiones_sistema_version", "versiones_sistema", ["version"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_versiones_sistema_version", table_name="versiones_sistema")
    op.drop_table("versiones_sistema")
    for column in reversed([
        "actualizacion_automatica",
        "actualizacion_estado",
        "actualizacion_mensaje",
        "actualizacion_fecha",
        "ultimo_respaldo",
    ]):
        op.drop_column("instalaciones_sistema", column)
