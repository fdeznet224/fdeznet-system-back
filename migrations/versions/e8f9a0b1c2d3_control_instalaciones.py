"""agrega control de instalaciones y versiones

Revision ID: e8f9a0b1c2d3
Revises: d7e8f9a0b1c2
Create Date: 2026-09-08
"""

from alembic import op
import sqlalchemy as sa


revision = "e8f9a0b1c2d3"
down_revision = "d7e8f9a0b1c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("configuracion_sistema", sa.Column("licencia_estado", sa.String(30), nullable=False, server_default="sin_configurar"))
    op.add_column("configuracion_sistema", sa.Column("licencia_ultima_revision", sa.DateTime(), nullable=True))
    op.add_column("configuracion_sistema", sa.Column("licencia_mensaje", sa.String(300), nullable=True))
    op.add_column("configuracion_sistema", sa.Column("version_disponible", sa.String(30), nullable=True))
    op.add_column("configuracion_sistema", sa.Column("notas_actualizacion", sa.Text(), nullable=True))
    op.create_table(
        "instalaciones_sistema",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("instalacion_id", sa.String(36), nullable=False),
        sa.Column("nombre_isp", sa.String(120), nullable=False),
        sa.Column("dominio", sa.String(255), nullable=True),
        sa.Column("contacto_email", sa.String(160), nullable=True),
        sa.Column("licencia_hash", sa.String(64), nullable=False),
        sa.Column("estado", sa.String(30), nullable=False, server_default="activa"),
        sa.Column("plan", sa.String(50), nullable=False, server_default="estandar"),
        sa.Column("canal", sa.String(30), nullable=False, server_default="stable"),
        sa.Column("version_actual", sa.String(30), nullable=True),
        sa.Column("version_objetivo", sa.String(30), nullable=True),
        sa.Column("notas_actualizacion", sa.Text(), nullable=True),
        sa.Column("ultima_conexion", sa.DateTime(), nullable=True),
        sa.Column("creada_en", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("actualizada_en", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("instalacion_id"),
        sa.UniqueConstraint("licencia_hash"),
    )
    op.create_index("ix_instalaciones_sistema_instalacion_id", "instalaciones_sistema", ["instalacion_id"], unique=True)
    op.create_index("ix_instalaciones_estado_ultima_conexion", "instalaciones_sistema", ["estado", "ultima_conexion"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_instalaciones_estado_ultima_conexion", table_name="instalaciones_sistema")
    op.drop_index("ix_instalaciones_sistema_instalacion_id", table_name="instalaciones_sistema")
    op.drop_table("instalaciones_sistema")
    for column in reversed([
        "licencia_estado",
        "licencia_ultima_revision",
        "licencia_mensaje",
        "version_disponible",
        "notas_actualizacion",
    ]):
        op.drop_column("configuracion_sistema", column)
