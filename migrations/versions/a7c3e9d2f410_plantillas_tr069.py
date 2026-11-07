"""Plantillas TR-069 por marca/modelo y estado de cada equipo frente a la suya

Revision ID: a7c3e9d2f410
Revises: 122cf117fc2f
Create Date: 2026-11-07
"""

from alembic import op
import sqlalchemy as sa


revision = "a7c3e9d2f410"
down_revision = "122cf117fc2f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plantillas_tr069",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("nombre", sa.String(80), nullable=False),
        sa.Column("marca", sa.String(20), nullable=False),
        sa.Column("modelos", sa.String(255), nullable=False, server_default=""),
        sa.Column("automatica", sa.Boolean, nullable=False, server_default="0"),
        sa.Column("definicion", sa.Text, nullable=False),
        sa.Column("creada_en", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("actualizada_en", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "equipos_tr069_estado",
        sa.Column("device_id", sa.String(160), primary_key=True),
        sa.Column("plantilla_id", sa.Integer, sa.ForeignKey("plantillas_tr069.id", ondelete="SET NULL"), nullable=True),
        sa.Column("conforme", sa.Boolean, nullable=False, server_default="0"),
        sa.Column("faltan", sa.Text, nullable=True),
        sa.Column("hecho", sa.Text, nullable=True),
        sa.Column("error", sa.String(255), nullable=True),
        sa.Column("revisado_en", sa.DateTime, nullable=True),
        sa.Column("aplicado_en", sa.DateTime, nullable=True),
    )


def downgrade() -> None:
    op.drop_table("equipos_tr069_estado")
    op.drop_table("plantillas_tr069")
