"""cada zona define su MikroTik, su OLT y su plantilla de cobro

Revision ID: 8e2f3a4b5c6d
Revises: 7d1e2a3b4c5f
Create Date: 2026-10-04
"""

from alembic import op
import sqlalchemy as sa


revision = "8e2f3a4b5c6d"
down_revision = "7d1e2a3b4c5f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("zonas") as tabla:
        tabla.add_column(sa.Column("router_id", sa.Integer(), nullable=True))
        tabla.add_column(sa.Column("olt_id", sa.Integer(), nullable=True))
        tabla.add_column(sa.Column("plantilla_id", sa.Integer(), nullable=True))
        tabla.create_foreign_key("fk_zonas_router", "routers", ["router_id"], ["id"], ondelete="SET NULL")
        tabla.create_foreign_key("fk_zonas_olt", "olts", ["olt_id"], ["id"], ondelete="SET NULL")
        tabla.create_foreign_key(
            "fk_zonas_plantilla", "plantillas_facturacion", ["plantilla_id"], ["id"], ondelete="SET NULL"
        )


def downgrade() -> None:
    with op.batch_alter_table("zonas") as tabla:
        tabla.drop_constraint("fk_zonas_plantilla", type_="foreignkey")
        tabla.drop_constraint("fk_zonas_olt", type_="foreignkey")
        tabla.drop_constraint("fk_zonas_router", type_="foreignkey")
        tabla.drop_column("plantilla_id")
        tabla.drop_column("olt_id")
        tabla.drop_column("router_id")
