"""expediente de bajas y recuperacion de equipos

Revision ID: d3f4a5b6c7d8
Revises: c2e3f4a5b6c7
Create Date: 2026-07-28
"""
from alembic import op
import sqlalchemy as sa


revision = "d3f4a5b6c7d8"
down_revision = "c2e3f4a5b6c7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "bajas_servicio",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("cliente_id", sa.Integer(), nullable=False),
        sa.Column("servicio_id", sa.Integer(), nullable=True),
        sa.Column("orden_retiro_id", sa.Integer(), nullable=True),
        sa.Column("onu_id", sa.Integer(), nullable=True),
        sa.Column("solicitada_por_id", sa.Integer(), nullable=True),
        sa.Column("tecnico_id", sa.Integer(), nullable=True),
        sa.Column(
            "estado",
            sa.String(30),
            server_default="pendiente_retiro",
            nullable=False,
        ),
        sa.Column("motivo", sa.String(500), nullable=False),
        sa.Column("observaciones", sa.Text(), nullable=True),
        sa.Column(
            "deuda_al_baja",
            sa.Numeric(12, 2),
            server_default="0.00",
            nullable=False,
        ),
        sa.Column("condicion_equipo", sa.String(30), nullable=True),
        sa.Column(
            "mikrotik_estado",
            sa.String(20),
            server_default="pendiente",
            nullable=False,
        ),
        sa.Column("mikrotik_error", sa.Text(), nullable=True),
        sa.Column("ip_snapshot", sa.String(20), nullable=True),
        sa.Column("caja_nap_id_snapshot", sa.Integer(), nullable=True),
        sa.Column("puerto_nap_snapshot", sa.Integer(), nullable=True),
        sa.Column("servicio_estado_snapshot", sa.String(30), nullable=True),
        sa.Column("proxima_facturacion_snapshot", sa.Date(), nullable=True),
        sa.Column(
            "solicitada_en",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("recuperada_en", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelada_en", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["cliente_id"],
            ["clientes.id"],
            name="fk_baja_cliente",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["servicio_id"],
            ["servicios.id"],
            name="fk_baja_servicio",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["orden_retiro_id"],
            ["ordenes_servicio.id"],
            name="fk_baja_orden_retiro",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["onu_id"],
            ["inventario_onus.id"],
            name="fk_baja_onu",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["solicitada_por_id"],
            ["usuarios.id"],
            name="fk_baja_solicitada_por",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["tecnico_id"],
            ["usuarios.id"],
            name="fk_baja_tecnico",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "orden_retiro_id",
            name="uq_bajas_servicio_orden_retiro",
        ),
    )
    op.create_index(
        "ix_bajas_servicio_cliente_id",
        "bajas_servicio",
        ["cliente_id"],
    )
    op.create_index(
        "ix_bajas_servicio_tecnico_id",
        "bajas_servicio",
        ["tecnico_id"],
    )
    op.create_index(
        "ix_bajas_cliente_estado",
        "bajas_servicio",
        ["cliente_id", "estado"],
    )
    op.create_index(
        "ix_bajas_tecnico_estado",
        "bajas_servicio",
        ["tecnico_id", "estado"],
    )
    op.execute(
        sa.text(
            """
            INSERT INTO bajas_servicio (
                cliente_id,
                servicio_id,
                orden_retiro_id,
                onu_id,
                tecnico_id,
                estado,
                motivo,
                observaciones,
                deuda_al_baja,
                mikrotik_estado,
                ip_snapshot,
                servicio_estado_snapshot,
                proxima_facturacion_snapshot
            )
            SELECT
                c.id,
                (
                    SELECT s.id
                    FROM servicios AS s
                    WHERE s.cliente_id = c.id
                    ORDER BY s.id DESC
                    LIMIT 1
                ),
                (
                    SELECT o.id
                    FROM ordenes_servicio AS o
                    WHERE o.cliente_id = c.id
                      AND o.tipo = 'retiro'
                      AND o.estado NOT IN ('terminada', 'cancelada')
                    ORDER BY o.id DESC
                    LIMIT 1
                ),
                c.onu_id,
                (
                    SELECT i.tecnico_id
                    FROM inventario_onus AS i
                    WHERE i.id = c.onu_id
                ),
                CASE
                    WHEN c.onu_id IS NULL THEN 'sin_equipo'
                    ELSE 'pendiente_retiro'
                END,
                'Baja registrada antes del expediente formal',
                'Registro migrado; la reactivación requiere una nueva instalación',
                COALESCE((
                    SELECT SUM(f.saldo_pendiente)
                    FROM facturas AS f
                    WHERE f.cliente_id = c.id
                      AND f.saldo_pendiente > 0
                      AND f.estado IN ('pendiente', 'vencida', 'promesa')
                ), 0),
                'desconocido',
                c.ip_asignada,
                'cancelado',
                c.proxima_factura
            FROM clientes AS c
            WHERE c.estado = 'cancelado'
            """
        )
    )


def downgrade():
    op.drop_index("ix_bajas_tecnico_estado", table_name="bajas_servicio")
    op.drop_index("ix_bajas_cliente_estado", table_name="bajas_servicio")
    op.drop_index("ix_bajas_servicio_tecnico_id", table_name="bajas_servicio")
    op.drop_index("ix_bajas_servicio_cliente_id", table_name="bajas_servicio")
    op.drop_table("bajas_servicio")
