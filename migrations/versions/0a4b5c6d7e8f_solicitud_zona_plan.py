"""la solicitud de instalación guarda la zona y el plan que pidió el prospecto

Revision ID: 0a4b5c6d7e8f
Revises: 9f3a4b5c6d7e
Create Date: 2026-10-04
"""

from alembic import op
import sqlalchemy as sa


revision = "0a4b5c6d7e8f"
down_revision = "9f3a4b5c6d7e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ordenes_servicio", sa.Column("zona_id", sa.Integer(), nullable=True))
    op.add_column("ordenes_servicio", sa.Column("plan_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_ordenes_servicio_zona", "ordenes_servicio", "zonas", ["zona_id"], ["id"], ondelete="SET NULL"
    )
    op.create_foreign_key(
        "fk_ordenes_servicio_plan", "ordenes_servicio", "planes", ["plan_id"], ["id"], ondelete="SET NULL"
    )


def downgrade() -> None:
    op.drop_constraint("fk_ordenes_servicio_plan", "ordenes_servicio", type_="foreignkey")
    op.drop_constraint("fk_ordenes_servicio_zona", "ordenes_servicio", type_="foreignkey")
    op.drop_column("ordenes_servicio", "plan_id")
    op.drop_column("ordenes_servicio", "zona_id")
