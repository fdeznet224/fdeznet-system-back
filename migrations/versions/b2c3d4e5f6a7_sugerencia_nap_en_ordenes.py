"""agrega NAP y puerto sugeridos a las órdenes sin reservarlos"""

from alembic import op
import sqlalchemy as sa

revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("ordenes_servicio", sa.Column("caja_nap_sugerida_id", sa.Integer(), nullable=True))
    op.add_column("ordenes_servicio", sa.Column("puerto_nap_sugerido", sa.Integer(), nullable=True))
    op.create_index("ix_ordenes_caja_nap_sugerida_id", "ordenes_servicio", ["caja_nap_sugerida_id"])
    op.create_foreign_key(
        "fk_ordenes_caja_nap_sugerida",
        "ordenes_servicio",
        "cajas_nap",
        ["caja_nap_sugerida_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade():
    op.drop_constraint("fk_ordenes_caja_nap_sugerida", "ordenes_servicio", type_="foreignkey")
    op.drop_index("ix_ordenes_caja_nap_sugerida_id", table_name="ordenes_servicio")
    op.drop_column("ordenes_servicio", "puerto_nap_sugerido")
    op.drop_column("ordenes_servicio", "caja_nap_sugerida_id")
