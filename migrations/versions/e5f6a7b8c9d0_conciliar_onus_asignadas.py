"""Conciliar estado de ONUs ya asignadas.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
"""

from alembic import op


revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        UPDATE inventario_onus AS i
        SET i.estado = 'INSTALADO'
        WHERE i.estado = 'DISPONIBLE'
          AND (
            EXISTS (
              SELECT 1
              FROM servicios AS s
              WHERE s.onu_id = i.id
                AND s.estado IN ('activo', 'suspendido')
            )
            OR EXISTS (
              SELECT 1
              FROM clientes AS c
              WHERE c.onu_id = i.id
                AND c.estado IN ('activo', 'suspendido')
            )
          )
        """
    )
    op.execute(
        """
        UPDATE inventario_onus AS i
        SET i.estado = 'RESERVADO'
        WHERE i.estado = 'DISPONIBLE'
          AND (
            EXISTS (
              SELECT 1
              FROM servicios AS s
              WHERE s.onu_id = i.id
                AND s.estado = 'pendiente_instalacion'
            )
            OR EXISTS (
              SELECT 1
              FROM clientes AS c
              WHERE c.onu_id = i.id
                AND c.estado = 'pendiente_instalacion'
            )
          )
        """
    )


def downgrade():
    # Es una conciliación de datos reales; revertirla volvería a ofrecer
    # equipos ocupados como disponibles.
    pass
