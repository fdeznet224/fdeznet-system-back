"""OLT y puerto PON de cada lectura óptica

Para ver si los clientes de una misma troncal (PON) bajaron de señal al mismo
tiempo. La API de VSOL dice el PON de cada ONU; por SNMP queda vacío.

Revision ID: 7c2d3e4f5a6b
Revises: 6b1c2d3e4f5a
Create Date: 2026-10-13
"""

from alembic import op
import sqlalchemy as sa


revision = "7c2d3e4f5a6b"
down_revision = "6b1c2d3e4f5a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("lecturas_opticas", sa.Column("olt_id", sa.Integer(), nullable=True))
    op.add_column("lecturas_opticas", sa.Column("pon", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_lecturas_opticas_olt", "lecturas_opticas", "olts", ["olt_id"], ["id"], ondelete="SET NULL"
    )
    op.create_index("ix_lecturas_opticas_fecha", "lecturas_opticas", ["fecha"])


def downgrade() -> None:
    op.drop_index("ix_lecturas_opticas_fecha", table_name="lecturas_opticas")
    op.drop_constraint("fk_lecturas_opticas_olt", "lecturas_opticas", type_="foreignkey")
    op.drop_column("lecturas_opticas", "pon")
    op.drop_column("lecturas_opticas", "olt_id")
