"""agrega cargo de reconexion configurable a plantillas

Revision ID: f3a4b5c6d7e8
Revises: e7f8a9b0c1d2
Create Date: 2026-09-07
"""

from alembic import op
import sqlalchemy as sa


revision = "f3a4b5c6d7e8"
down_revision = "e7f8a9b0c1d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "plantillas_facturacion",
        sa.Column(
            "cargo_reconexion",
            sa.Numeric(12, 2),
            nullable=False,
            server_default=sa.text("30.00"),
        ),
    )
    op.execute(
        """
        UPDATE plantillas_facturacion
        SET dias_antes_emision = 5,
            dias_tolerancia = 10,
            cargo_reconexion = 30.00
        WHERE dia_pago IN (1, 15)
        """
    )


def downgrade() -> None:
    op.drop_column("plantillas_facturacion", "cargo_reconexion")
