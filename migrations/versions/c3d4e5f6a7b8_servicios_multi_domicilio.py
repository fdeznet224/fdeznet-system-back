"""convierte servicios en contratos técnicos por domicilio

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-07-29
"""

from alembic import op
import sqlalchemy as sa


revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade():
    columnas_servicio = (
        sa.Column(
            "alias",
            sa.String(100),
            nullable=False,
            server_default="Principal",
        ),
        sa.Column("direccion", sa.String(255), nullable=True),
        sa.Column("latitud", sa.Float(), nullable=True),
        sa.Column("longitud", sa.Float(), nullable=True),
        sa.Column("router_id", sa.Integer(), nullable=True),
        sa.Column("zona_id", sa.Integer(), nullable=True),
        sa.Column("red_id", sa.Integer(), nullable=True),
        sa.Column("olt_id", sa.Integer(), nullable=True),
        sa.Column("caja_nap_id", sa.Integer(), nullable=True),
        sa.Column("puerto_nap", sa.Integer(), nullable=True),
        sa.Column("tecnico_id", sa.Integer(), nullable=True),
        sa.Column("onu_id", sa.Integer(), nullable=True),
        sa.Column("ip_asignada", sa.String(20), nullable=True),
        sa.Column("mac_address", sa.String(20), nullable=True),
        sa.Column("user_pppoe", sa.String(50), nullable=True),
        sa.Column("pass_pppoe", sa.String(100), nullable=True),
        sa.Column(
            "is_online",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("ultimo_cambio_estado", sa.DateTime(), nullable=True),
    )
    for columna in columnas_servicio:
        op.add_column("servicios", columna)

    foreign_keys = (
        ("fk_servicios_router", "router_id", "routers"),
        ("fk_servicios_zona", "zona_id", "zonas"),
        ("fk_servicios_red", "red_id", "redes"),
        ("fk_servicios_olt", "olt_id", "olts"),
        ("fk_servicios_caja_nap", "caja_nap_id", "cajas_nap"),
        ("fk_servicios_tecnico", "tecnico_id", "usuarios"),
        ("fk_servicios_onu", "onu_id", "inventario_onus"),
    )
    for nombre, columna, tabla in foreign_keys:
        op.create_foreign_key(
            nombre,
            "servicios",
            tabla,
            [columna],
            ["id"],
            ondelete="SET NULL",
        )

    for columna in (
        "router_id",
        "zona_id",
        "red_id",
        "olt_id",
        "caja_nap_id",
        "tecnico_id",
        "onu_id",
        "user_pppoe",
    ):
        op.create_index(
            f"ix_servicios_{columna}",
            "servicios",
            [columna],
        )
    op.create_index(
        "ix_servicios_ip_asignada",
        "servicios",
        ["ip_asignada"],
        unique=True,
    )
    op.create_index(
        "ix_servicios_router_estado",
        "servicios",
        ["router_id", "estado"],
    )
    op.create_unique_constraint(
        "uq_servicios_onu_id",
        "servicios",
        ["onu_id"],
    )
    op.create_unique_constraint(
        "uq_servicios_nap_puerto",
        "servicios",
        ["caja_nap_id", "puerto_nap"],
    )

    # El servicio inicial hereda íntegramente la instalación histórica.
    op.execute(
        """
        UPDATE servicios AS s
        INNER JOIN clientes AS c ON c.id = s.cliente_id
        INNER JOIN (
            SELECT cliente_id, MIN(id) AS servicio_principal_id
            FROM servicios
            GROUP BY cliente_id
        ) AS principal ON principal.servicio_principal_id = s.id
        SET
            s.alias = 'Principal',
            s.direccion = c.direccion,
            s.latitud = c.latitud,
            s.longitud = c.longitud,
            s.router_id = c.router_id,
            s.zona_id = c.zona_id,
            s.red_id = c.red_id,
            s.olt_id = c.olt_id,
            s.caja_nap_id = c.caja_nap_id,
            s.puerto_nap = c.puerto_nap,
            s.tecnico_id = c.tecnico_id,
            s.onu_id = c.onu_id,
            s.ip_asignada = NULLIF(c.ip_asignada, '0.0.0.0'),
            s.mac_address = c.mac_address,
            s.user_pppoe = c.user_pppoe,
            s.pass_pppoe = c.pass_pppoe,
            s.is_online = COALESCE(c.is_online, 0),
            s.ultimo_cambio_estado = c.ultimo_cambio_estado
        """
    )

    op.add_column(
        "ordenes_servicio",
        sa.Column("servicio_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_ordenes_servicio_id",
        "ordenes_servicio",
        ["servicio_id"],
    )
    op.create_foreign_key(
        "fk_ordenes_servicio",
        "ordenes_servicio",
        "servicios",
        ["servicio_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute(
        """
        UPDATE ordenes_servicio AS o
        INNER JOIN (
            SELECT cliente_id, MIN(id) AS servicio_principal_id
            FROM servicios
            GROUP BY cliente_id
        ) AS principal ON principal.cliente_id = o.cliente_id
        SET o.servicio_id = principal.servicio_principal_id
        WHERE o.servicio_id IS NULL
        """
    )

    for tabla in (
        "puertos_nap",
        "historial_equipos",
        "lecturas_opticas",
        "diagnosticos_soporte",
    ):
        op.add_column(
            tabla,
            sa.Column("servicio_id", sa.Integer(), nullable=True),
        )
        op.create_index(
            f"ix_{tabla}_servicio_id",
            tabla,
            ["servicio_id"],
        )
        op.create_foreign_key(
            f"fk_{tabla}_servicio",
            tabla,
            "servicios",
            ["servicio_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.execute(
            f"""
            UPDATE {tabla} AS t
            INNER JOIN (
                SELECT cliente_id, MIN(id) AS servicio_principal_id
                FROM servicios
                GROUP BY cliente_id
            ) AS principal ON principal.cliente_id = t.cliente_id
            SET t.servicio_id = principal.servicio_principal_id
            WHERE t.servicio_id IS NULL
            """
        )

    # MySQL puede usar el índice único anterior como soporte de la FK
    # cliente_id. Crear primero uno normal permite retirar la unicidad sin
    # invalidar esa clave foránea.
    op.create_index(
        "ix_puertos_nap_cliente_id",
        "puertos_nap",
        ["cliente_id"],
    )
    op.drop_constraint(
        "uq_puertos_nap_cliente_id",
        "puertos_nap",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_puertos_nap_servicio_id",
        "puertos_nap",
        ["servicio_id"],
    )


def downgrade():
    op.drop_constraint(
        "uq_puertos_nap_servicio_id",
        "puertos_nap",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_puertos_nap_cliente_id",
        "puertos_nap",
        ["cliente_id"],
    )
    op.drop_index(
        "ix_puertos_nap_cliente_id",
        table_name="puertos_nap",
    )

    for tabla in (
        "diagnosticos_soporte",
        "lecturas_opticas",
        "historial_equipos",
        "puertos_nap",
    ):
        op.drop_constraint(
            f"fk_{tabla}_servicio",
            tabla,
            type_="foreignkey",
        )
        op.drop_index(
            f"ix_{tabla}_servicio_id",
            table_name=tabla,
        )
        op.drop_column(tabla, "servicio_id")

    op.drop_constraint(
        "fk_ordenes_servicio",
        "ordenes_servicio",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_ordenes_servicio_id",
        table_name="ordenes_servicio",
    )
    op.drop_column("ordenes_servicio", "servicio_id")

    op.drop_constraint(
        "uq_servicios_nap_puerto",
        "servicios",
        type_="unique",
    )
    op.drop_constraint(
        "uq_servicios_onu_id",
        "servicios",
        type_="unique",
    )
    op.drop_index(
        "ix_servicios_router_estado",
        table_name="servicios",
    )
    op.drop_index(
        "ix_servicios_ip_asignada",
        table_name="servicios",
    )
    for columna in (
        "user_pppoe",
        "onu_id",
        "tecnico_id",
        "caja_nap_id",
        "olt_id",
        "red_id",
        "zona_id",
        "router_id",
    ):
        op.drop_index(
            f"ix_servicios_{columna}",
            table_name="servicios",
        )

    for nombre, _, _ in reversed(
        (
            ("fk_servicios_router", "router_id", "routers"),
            ("fk_servicios_zona", "zona_id", "zonas"),
            ("fk_servicios_red", "red_id", "redes"),
            ("fk_servicios_olt", "olt_id", "olts"),
            ("fk_servicios_caja_nap", "caja_nap_id", "cajas_nap"),
            ("fk_servicios_tecnico", "tecnico_id", "usuarios"),
            ("fk_servicios_onu", "onu_id", "inventario_onus"),
        )
    ):
        op.drop_constraint(nombre, "servicios", type_="foreignkey")

    for columna in (
        "ultimo_cambio_estado",
        "is_online",
        "pass_pppoe",
        "user_pppoe",
        "mac_address",
        "ip_asignada",
        "onu_id",
        "tecnico_id",
        "puerto_nap",
        "caja_nap_id",
        "olt_id",
        "red_id",
        "zona_id",
        "router_id",
        "longitud",
        "latitud",
        "direccion",
        "alias",
    ):
        op.drop_column("servicios", columna)
