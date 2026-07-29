"""soporte tecnico y diagnosticos historicos

Revision ID: b1d2e3f4a5b6
Revises: a0c1d2e3f4a5
Create Date: 2026-07-28
"""
from alembic import op
import sqlalchemy as sa


revision = "b1d2e3f4a5b6"
down_revision = "a0c1d2e3f4a5"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "ordenes_servicio",
        sa.Column("categoria_soporte", sa.String(30), nullable=True),
    )
    op.add_column(
        "ordenes_servicio",
        sa.Column(
            "canal_reporte",
            sa.String(20),
            server_default="panel",
            nullable=False,
        ),
    )
    op.add_column(
        "ordenes_servicio",
        sa.Column("tiempo_primera_respuesta_minutos", sa.Integer(), nullable=True),
    )
    op.add_column(
        "ordenes_servicio",
        sa.Column("tiempo_resolucion_minutos", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_ordenes_servicio_categoria_soporte",
        "ordenes_servicio",
        ["categoria_soporte"],
    )

    op.create_table(
        "diagnosticos_soporte",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("orden_id", sa.Integer(), nullable=False),
        sa.Column("cliente_id", sa.Integer(), nullable=True),
        sa.Column("ejecutado_por_id", sa.Integer(), nullable=True),
        sa.Column("resultado", sa.String(20), nullable=False),
        sa.Column("codigo_sugerencia", sa.String(50), nullable=False),
        sa.Column("sugerencia", sa.Text(), nullable=False),
        sa.Column(
            "mikrotik_disponible",
            sa.Boolean(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("pppoe_online", sa.Boolean(), nullable=True),
        sa.Column("ip_actual", sa.String(45), nullable=True),
        sa.Column("uptime", sa.String(50), nullable=True),
        sa.Column("mac_reportada", sa.String(50), nullable=True),
        sa.Column("ping_estado", sa.String(20), nullable=True),
        sa.Column(
            "perdida_paquetes_porcentaje",
            sa.Numeric(5, 2),
            nullable=True,
        ),
        sa.Column("trafico_subida_bps", sa.BigInteger(), nullable=True),
        sa.Column("trafico_bajada_bps", sa.BigInteger(), nullable=True),
        sa.Column(
            "olt_disponible",
            sa.Boolean(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("onu_online", sa.Boolean(), nullable=True),
        sa.Column("potencia_rx_dbm", sa.Numeric(6, 2), nullable=True),
        sa.Column("potencia_tx_dbm", sa.Numeric(6, 2), nullable=True),
        sa.Column("origen_olt", sa.String(20), nullable=True),
        sa.Column("errores", sa.Text(), nullable=True),
        sa.Column("datos_crudos", sa.Text(), nullable=True),
        sa.Column(
            "fecha",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["orden_id"],
            ["ordenes_servicio.id"],
            name="fk_diagnostico_orden",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["cliente_id"],
            ["clientes.id"],
            name="fk_diagnostico_cliente",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["ejecutado_por_id"],
            ["usuarios.id"],
            name="fk_diagnostico_usuario",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_diagnosticos_soporte_orden_id",
        "diagnosticos_soporte",
        ["orden_id"],
    )
    op.create_index(
        "ix_diagnosticos_soporte_cliente_id",
        "diagnosticos_soporte",
        ["cliente_id"],
    )
    op.create_index(
        "ix_diagnosticos_orden_fecha",
        "diagnosticos_soporte",
        ["orden_id", "fecha"],
    )
    op.create_index(
        "ix_diagnosticos_cliente_fecha",
        "diagnosticos_soporte",
        ["cliente_id", "fecha"],
    )


def downgrade():
    op.drop_table("diagnosticos_soporte")
    op.drop_index(
        "ix_ordenes_servicio_categoria_soporte",
        table_name="ordenes_servicio",
    )
    op.drop_column("ordenes_servicio", "tiempo_resolucion_minutos")
    op.drop_column("ordenes_servicio", "tiempo_primera_respuesta_minutos")
    op.drop_column("ordenes_servicio", "canal_reporte")
    op.drop_column("ordenes_servicio", "categoria_soporte")
