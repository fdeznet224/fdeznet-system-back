"""crear servicios isp

Revision ID: 2316a59f3ffa
Revises: 917b268f5a2d
Create Date: 2026-07-07 22:38:36.213385

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2316a59f3ffa'
down_revision: Union[str, Sequence[str], None] = '917b268f5a2d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "servicios",
        sa.Column(
            "id",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "cliente_id",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "plan_id",
            sa.Integer(),
            nullable=True,
        ),
        sa.Column(
            "plantilla_id",
            sa.Integer(),
            nullable=True,
        ),
        sa.Column(
            "tipo_facturacion",
            sa.String(length=20),
            server_default="prepago",
            nullable=False,
        ),
        sa.Column(
            "ciclo_facturacion",
            sa.String(length=20),
            server_default="calendario",
            nullable=False,
        ),
        sa.Column(
            "fecha_instalacion",
            sa.Date(),
            nullable=True,
        ),
        sa.Column(
            "fecha_activacion",
            sa.Date(),
            nullable=True,
        ),
        sa.Column(
            "fecha_inicio_servicio",
            sa.Date(),
            nullable=True,
        ),
        sa.Column(
            "fecha_fin_periodo_gratis",
            sa.Date(),
            nullable=True,
        ),
        sa.Column(
            "fecha_inicio_cobro",
            sa.Date(),
            nullable=True,
        ),
        sa.Column(
            "proxima_facturacion",
            sa.Date(),
            nullable=True,
        ),
        sa.Column(
            "dia_vencimiento",
            sa.Integer(),
            nullable=True,
        ),
        sa.Column(
            "dias_tolerancia",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "meses_gratis",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "politica_prorrateo",
            sa.String(length=30),
            server_default="dias_reales_mes",
            nullable=False,
        ),
        sa.Column(
            "estado",
            sa.String(length=30),
            server_default="pendiente_instalacion",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["cliente_id"],
            ["clientes.id"],
            name="fk_servicios_cliente_id_clientes",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["planes.id"],
            name="fk_servicios_plan_id_planes",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["plantilla_id"],
            ["plantillas_facturacion.id"],
            name="fk_servicios_plantilla_id_plantillas",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name="pk_servicios",
        ),
    )

    op.create_index(
        "ix_servicios_cliente_id",
        "servicios",
        ["cliente_id"],
        unique=False,
    )

    op.create_index(
        "ix_servicios_plan_id",
        "servicios",
        ["plan_id"],
        unique=False,
    )

    op.create_index(
        "ix_servicios_plantilla_id",
        "servicios",
        ["plantilla_id"],
        unique=False,
    )

    op.create_index(
        "ix_servicios_cliente_estado",
        "servicios",
        ["cliente_id", "estado"],
        unique=False,
    )

    op.create_index(
        "ix_servicios_proxima_facturacion",
        "servicios",
        ["proxima_facturacion"],
        unique=False,
    )

    # Crea un servicio inicial para cada cliente existente.
    # No inventamos fechas de instalación porque actualmente
    # el sistema no tiene ese dato histórico.
    op.execute(
        """
        INSERT INTO servicios (
            cliente_id,
            plan_id,
            plantilla_id,
            tipo_facturacion,
            ciclo_facturacion,
            proxima_facturacion,
            dia_vencimiento,
            dias_tolerancia,
            meses_gratis,
            politica_prorrateo,
            estado,
            created_at,
            updated_at
        )
        SELECT
            c.id,
            c.plan_id,
            c.plantilla_id,
            'prepago',
            'calendario',
            c.proxima_factura,
            pf.dia_pago,
            COALESCE(pf.dias_tolerancia, 0),
            0,
            'dias_reales_mes',
            CASE
                WHEN c.estado = 'activo' THEN 'activo'
                WHEN c.estado = 'suspendido' THEN 'suspendido'
                WHEN c.estado = 'cancelado' THEN 'cancelado'
                ELSE 'pendiente_instalacion'
            END,
            CURRENT_TIMESTAMP,
            CURRENT_TIMESTAMP
        FROM clientes AS c
        LEFT JOIN plantillas_facturacion AS pf
            ON pf.id = c.plantilla_id
        """
    )

    op.add_column(
        "facturas",
        sa.Column(
            "servicio_id",
            sa.Integer(),
            nullable=True,
        ),
    )

    op.create_index(
        "ix_facturas_servicio_id",
        "facturas",
        ["servicio_id"],
        unique=False,
    )

    op.create_foreign_key(
        "fk_facturas_servicio_id_servicios",
        "facturas",
        "servicios",
        ["servicio_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # Relaciona las facturas existentes con el servicio
    # inicial creado para cada cliente.
    op.execute(
        """
        UPDATE facturas AS f
        INNER JOIN servicios AS s
            ON s.cliente_id = f.cliente_id
        SET f.servicio_id = s.id
        WHERE f.servicio_id IS NULL
        """
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_facturas_servicio_id_servicios",
        "facturas",
        type_="foreignkey",
    )

    op.drop_index(
        "ix_facturas_servicio_id",
        table_name="facturas",
    )

    op.drop_column(
        "facturas",
        "servicio_id",
    )

    op.drop_index(
        "ix_servicios_proxima_facturacion",
        table_name="servicios",
    )

    op.drop_index(
        "ix_servicios_cliente_estado",
        table_name="servicios",
    )

    op.drop_index(
        "ix_servicios_plantilla_id",
        table_name="servicios",
    )

    op.drop_index(
        "ix_servicios_plan_id",
        table_name="servicios",
    )

    op.drop_index(
        "ix_servicios_cliente_id",
        table_name="servicios",
    )

    op.drop_table("servicios")
