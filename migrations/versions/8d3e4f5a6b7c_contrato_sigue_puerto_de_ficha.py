"""El contrato vuelve al puerto NAP de la ficha del cliente

Editar el puerto en la ficha sólo cambiaba al cliente: su único contrato se
quedaba en el puerto viejo y la caja lo seguía mostrando ocupado. Se alinea
el contrato con la ficha y se sueltan los puertos de contratos cancelados.

Revision ID: 8d3e4f5a6b7c
Revises: 7c2d3e4f5a6b
Create Date: 2026-10-17
"""

from alembic import op
import sqlalchemy as sa


revision = "8d3e4f5a6b7c"
down_revision = "7c2d3e4f5a6b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    desfasados = conn.execute(
        sa.text(
            """
            SELECT s.id, c.caja_nap_id, c.puerto_nap
            FROM servicios s
            JOIN clientes c ON c.id = s.cliente_id
            WHERE s.estado <> 'cancelado'
              AND NOT (s.caja_nap_id <=> c.caja_nap_id
                       AND s.puerto_nap <=> c.puerto_nap)
              AND (SELECT COUNT(*) FROM servicios o
                   WHERE o.cliente_id = c.id AND o.estado <> 'cancelado') = 1
            """
        )
    ).fetchall()
    cancelados = [
        fila[0]
        for fila in conn.execute(
            sa.text(
                "SELECT id FROM servicios WHERE estado = 'cancelado' "
                "AND puerto_nap IS NOT NULL"
            )
        )
    ]
    ids = [fila[0] for fila in desfasados] + cancelados
    if not ids:
        return

    liberar = sa.text(
        "UPDATE puertos_nap SET estado = 'libre', servicio_id = NULL, "
        "cliente_id = NULL WHERE servicio_id IN :ids"
    ).bindparams(sa.bindparam("ids", expanding=True))
    conn.execute(liberar, {"ids": ids})
    vaciar = sa.text(
        "UPDATE servicios SET caja_nap_id = NULL, puerto_nap = NULL "
        "WHERE id IN :ids"
    ).bindparams(sa.bindparam("ids", expanding=True))
    conn.execute(vaciar, {"ids": ids})

    for servicio_id, caja_nap_id, puerto_nap in desfasados:
        if caja_nap_id is None or puerto_nap is None:
            continue
        ocupado = conn.execute(
            sa.text(
                "SELECT 1 FROM servicios WHERE caja_nap_id = :caja "
                "AND puerto_nap = :puerto"
            ),
            {"caja": caja_nap_id, "puerto": puerto_nap},
        ).first()
        if ocupado:
            continue
        conn.execute(
            sa.text(
                "UPDATE servicios SET caja_nap_id = :caja, "
                "puerto_nap = :puerto WHERE id = :id"
            ),
            {"caja": caja_nap_id, "puerto": puerto_nap, "id": servicio_id},
        )


def downgrade() -> None:
    # Los puertos viejos eran justamente el error; no se restauran.
    pass
