"""el contrato vuelve a apuntar a la ONU que tiene el cliente

Hasta 2.84.3 el cambio de ONU solo movía la ficha del cliente: el servicio se
quedaba con la ONU retirada y, al reingresarla, el inventario la escondía como
ocupada. Solo se corrigen clientes con un único servicio vigente, donde no hay
duda de a qué contrato pertenece la ONU.

Revision ID: 5a0b1c2d3e4f
Revises: 4f9a0b1c2d3e
Create Date: 2026-10-11
"""

from alembic import op
import sqlalchemy as sa


revision = "5a0b1c2d3e4f"
down_revision = "4f9a0b1c2d3e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    desfasados = conn.execute(
        sa.text(
            """
            SELECT s.id AS servicio_id, c.onu_id AS onu_cliente
            FROM servicios s
            JOIN clientes c ON c.id = s.cliente_id
            WHERE s.estado <> 'cancelado'
              AND c.onu_id IS NOT NULL
              AND s.onu_id IS NOT NULL
              AND s.onu_id <> c.onu_id
              AND (
                  SELECT COUNT(*) FROM servicios s2
                  WHERE s2.cliente_id = c.id AND s2.estado <> 'cancelado'
              ) = 1
            """
        )
    ).fetchall()
    # Dos contratos desfasados pueden tener cruzadas sus ONU: se repite
    # mientras alguno se libere.
    pendientes = list(desfasados)
    while pendientes:
        quedan = []
        for servicio_id, onu_cliente in pendientes:
            ocupada = conn.execute(
                sa.text(
                    "SELECT 1 FROM servicios WHERE onu_id = :onu AND id <> :id "
                    "AND estado <> 'cancelado'"
                ),
                {"onu": onu_cliente, "id": servicio_id},
            ).first()
            if ocupada:
                quedan.append((servicio_id, onu_cliente))
                continue
            conn.execute(
                sa.text("UPDATE servicios SET onu_id = :onu WHERE id = :id"),
                {"onu": onu_cliente, "id": servicio_id},
            )
        if len(quedan) == len(pendientes):
            break
        pendientes = quedan


def downgrade() -> None:
    # Corrección de datos: no hay estado anterior que restaurar.
    pass
