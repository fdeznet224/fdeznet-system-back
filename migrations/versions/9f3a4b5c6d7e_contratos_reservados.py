"""contratos apartados para que el técnico conozca el número antes de instalar

Revision ID: 9f3a4b5c6d7e
Revises: 8e2f3a4b5c6d
Create Date: 2026-10-04
"""

from alembic import op
import sqlalchemy as sa


revision = "9f3a4b5c6d7e"
down_revision = "8e2f3a4b5c6d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "contratos_reservados",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("codigo", sa.String(length=20), nullable=False),
        sa.Column("usuario_id", sa.Integer(), sa.ForeignKey("usuarios.id", ondelete="CASCADE"), nullable=False),
        sa.Column("reservado_en", sa.DateTime(), nullable=False),
        sa.Column("usado_en", sa.DateTime(), nullable=True),
        sa.Column("cliente_id", sa.Integer(), sa.ForeignKey("clientes.id", ondelete="SET NULL"), nullable=True),
        sa.Column("liberado_en", sa.DateTime(), nullable=True),
    )
    op.create_index("ux_contratos_reservados_codigo", "contratos_reservados", ["codigo"], unique=True)
    op.create_index("ix_contratos_reservados_usuario", "contratos_reservados", ["usuario_id", "usado_en", "liberado_en"])


def downgrade() -> None:
    op.drop_index("ix_contratos_reservados_usuario", table_name="contratos_reservados")
    op.drop_index("ux_contratos_reservados_codigo", table_name="contratos_reservados")
    op.drop_table("contratos_reservados")
