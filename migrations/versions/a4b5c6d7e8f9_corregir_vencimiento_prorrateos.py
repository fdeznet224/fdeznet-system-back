"""corrige vencimiento de prorrateos iniciales pendientes

Revision ID: a4b5c6d7e8f9
Revises: f3a4b5c6d7e8
Create Date: 2026-09-07
"""

from alembic import op


revision = "a4b5c6d7e8f9"
down_revision = "f3a4b5c6d7e8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE facturas f
        JOIN clientes c ON c.id = f.cliente_id
        LEFT JOIN servicios s ON s.id = f.servicio_id
        JOIN plantillas_facturacion p
          ON p.id = COALESCE(s.plantilla_id, c.plantilla_id)
        SET f.fecha_vencimiento = DATE_ADD(f.periodo_hasta, INTERVAL 1 DAY),
            f.fecha_limite_corte = DATE_ADD(
                DATE_ADD(f.periodo_hasta, INTERVAL 1 DAY),
                INTERVAL p.dias_tolerancia DAY
            )
        WHERE f.tipo_factura = 'prorrateo'
          AND f.estado IN ('pendiente', 'vencida')
          AND f.saldo_pendiente > 0
          AND f.periodo_hasta IS NOT NULL
          AND f.fecha_vencimiento < DATE_ADD(f.periodo_hasta, INTERVAL 1 DAY)
        """
    )


def downgrade() -> None:
    # La fecha histórica anterior no puede reconstruirse con seguridad.
    pass
