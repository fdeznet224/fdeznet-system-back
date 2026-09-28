"""agrega modo de corte solo whatsapp

Revision ID: b3c4d5e6f7a9
Revises: a2b3c4d5e6f8
Create Date: 2026-09-27
"""

from alembic import op
import sqlalchemy as sa


revision = "b3c4d5e6f7a9"
down_revision = "a2b3c4d5e6f8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "configuracion_sistema",
        sa.Column(
            "corte_solo_whatsapp",
            sa.Boolean(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "configuracion_sistema",
        sa.Column(
            "corte_whatsapp_kbps",
            sa.Integer(),
            nullable=False,
            server_default="128",
        ),
    )


def downgrade() -> None:
    op.drop_column("configuracion_sistema", "corte_whatsapp_kbps")
    op.drop_column("configuracion_sistema", "corte_solo_whatsapp")
