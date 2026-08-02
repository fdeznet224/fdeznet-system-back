"""hereda configuracion de facturacion del cliente en servicios existentes

Revision ID: f0a1b2c3d4e5
Revises: e5f6a7b8c9d0
Create Date: 2026-08-01
"""

from alembic import op


revision = "f0a1b2c3d4e5"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # La migración de servicios pudo dejar fuera datos comerciales. Sólo
    # rellenamos campos nulos para respetar configuraciones propias.
    op.execute(
        """
        UPDATE servicios AS s
        INNER JOIN clientes AS c ON c.id = s.cliente_id
        SET
            s.plantilla_id = COALESCE(s.plantilla_id, c.plantilla_id),
            s.plan_id = COALESCE(s.plan_id, c.plan_id)
        WHERE (s.plantilla_id IS NULL AND c.plantilla_id IS NOT NULL)
           OR (s.plan_id IS NULL AND c.plan_id IS NOT NULL)
        """
    )


def downgrade() -> None:
    # No es seguro revertir sólo las filas heredadas porque no se conserva
    # cuáles fueron modificadas antes de esta migración.
    pass
