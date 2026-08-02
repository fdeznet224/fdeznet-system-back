"""ordenes tecnicas y control ftth

Revision ID: f9b0c1d2e3f4
Revises: e8a9b0c1d2e3
Create Date: 2026-07-28
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text


revision = "f9b0c1d2e3f4"
down_revision = "e8a9b0c1d2e3"
branch_labels = None
depends_on = None


def _assert_assignments_are_unique(bind):
    duplicated_onu = bind.execute(text("""
        SELECT onu_id
        FROM clientes
        WHERE onu_id IS NOT NULL
        GROUP BY onu_id
        HAVING COUNT(*) > 1
        LIMIT 1
    """)).first()
    if duplicated_onu:
        raise RuntimeError(
            f"La ONU {duplicated_onu[0]} está asignada a varios clientes"
        )

    duplicated_port = bind.execute(text("""
        SELECT caja_nap_id, puerto_nap
        FROM clientes
        WHERE caja_nap_id IS NOT NULL AND puerto_nap IS NOT NULL
        GROUP BY caja_nap_id, puerto_nap
        HAVING COUNT(*) > 1
        LIMIT 1
    """)).first()
    if duplicated_port:
        raise RuntimeError(
            "Hay clientes duplicados en la NAP "
            f"{duplicated_port[0]}, puerto {duplicated_port[1]}"
        )


def upgrade():
    bind = op.get_bind()
    _assert_assignments_are_unique(bind)

    op.create_table(
        "ordenes_servicio",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tipo", sa.String(30), nullable=False),
        sa.Column("cliente_id", sa.Integer(), nullable=True),
        sa.Column("prospecto_nombre", sa.String(150), nullable=True),
        sa.Column("prospecto_telefono", sa.String(20), nullable=True),
        sa.Column("prospecto_direccion", sa.String(255), nullable=True),
        sa.Column("tecnico_id", sa.Integer(), nullable=True),
        sa.Column("creado_por_id", sa.Integer(), nullable=True),
        sa.Column("prioridad", sa.String(20), server_default="normal", nullable=False),
        sa.Column("estado", sa.String(30), server_default="pendiente", nullable=False),
        sa.Column("fecha_programada", sa.DateTime(), nullable=True),
        sa.Column("fecha_inicio", sa.DateTime(), nullable=True),
        sa.Column("fecha_finalizacion", sa.DateTime(), nullable=True),
        sa.Column("fecha_cancelacion", sa.DateTime(), nullable=True),
        sa.Column("motivo", sa.String(100), nullable=True),
        sa.Column("descripcion", sa.Text(), nullable=True),
        sa.Column("diagnostico", sa.Text(), nullable=True),
        sa.Column("solucion", sa.Text(), nullable=True),
        sa.Column("conformidad_cliente", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
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
            name="fk_orden_cliente",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["tecnico_id"],
            ["usuarios.id"],
            name="fk_orden_tecnico",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["creado_por_id"],
            ["usuarios.id"],
            name="fk_orden_creador",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_ordenes_estado_programada",
        "ordenes_servicio",
        ["estado", "fecha_programada"],
    )
    op.create_index(
        "ix_ordenes_tecnico_estado",
        "ordenes_servicio",
        ["tecnico_id", "estado"],
    )
    op.create_index(
        "ix_ordenes_servicio_cliente_id",
        "ordenes_servicio",
        ["cliente_id"],
    )
    op.create_index(
        "ix_ordenes_servicio_tecnico_id",
        "ordenes_servicio",
        ["tecnico_id"],
    )

    op.create_table(
        "historial_estados_orden",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("orden_id", sa.Integer(), nullable=False),
        sa.Column("usuario_id", sa.Integer(), nullable=True),
        sa.Column("estado_anterior", sa.String(30), nullable=True),
        sa.Column("estado_nuevo", sa.String(30), nullable=False),
        sa.Column("comentario", sa.Text(), nullable=True),
        sa.Column(
            "fecha",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["orden_id"],
            ["ordenes_servicio.id"],
            name="fk_historial_orden",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["usuario_id"],
            ["usuarios.id"],
            name="fk_historial_usuario",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_historial_orden_fecha",
        "historial_estados_orden",
        ["orden_id", "fecha"],
    )

    op.create_table(
        "evidencias_orden",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("orden_id", sa.Integer(), nullable=False),
        sa.Column("usuario_id", sa.Integer(), nullable=True),
        sa.Column("tipo", sa.String(20), server_default="foto", nullable=False),
        sa.Column("nombre_original", sa.String(255), nullable=False),
        sa.Column("ruta_archivo", sa.String(500), nullable=False),
        sa.Column("mime_type", sa.String(100), nullable=False),
        sa.Column("tamano_bytes", sa.Integer(), nullable=False),
        sa.Column("comentario", sa.String(500), nullable=True),
        sa.Column(
            "fecha",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["orden_id"],
            ["ordenes_servicio.id"],
            name="fk_evidencia_orden",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["usuario_id"],
            ["usuarios.id"],
            name="fk_evidencia_usuario",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_evidencias_orden_orden_id",
        "evidencias_orden",
        ["orden_id"],
    )

    op.create_table(
        "materiales_orden",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("orden_id", sa.Integer(), nullable=False),
        sa.Column("descripcion", sa.String(150), nullable=False),
        sa.Column("cantidad", sa.Numeric(10, 2), nullable=False),
        sa.Column("unidad", sa.String(30), server_default="pieza", nullable=False),
        sa.Column("observaciones", sa.String(500), nullable=True),
        sa.ForeignKeyConstraint(
            ["orden_id"],
            ["ordenes_servicio.id"],
            name="fk_material_orden",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_materiales_orden_orden_id",
        "materiales_orden",
        ["orden_id"],
    )

    op.create_table(
        "puertos_nap",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("caja_nap_id", sa.Integer(), nullable=False),
        sa.Column("numero", sa.Integer(), nullable=False),
        sa.Column("estado", sa.String(20), server_default="libre", nullable=False),
        sa.Column("cliente_id", sa.Integer(), nullable=True),
        sa.Column("orden_id", sa.Integer(), nullable=True),
        sa.Column("potencia_instalacion_dbm", sa.Numeric(6, 2), nullable=True),
        sa.Column("observaciones", sa.String(500), nullable=True),
        sa.Column("actualizado_por_id", sa.Integer(), nullable=True),
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
            ["caja_nap_id"],
            ["cajas_nap.id"],
            name="fk_puerto_nap",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["cliente_id"],
            ["clientes.id"],
            name="fk_puerto_cliente",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["orden_id"],
            ["ordenes_servicio.id"],
            name="fk_puerto_orden",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["actualizado_por_id"],
            ["usuarios.id"],
            name="fk_puerto_usuario",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "caja_nap_id",
            "numero",
            name="uq_puertos_nap_caja_numero",
        ),
        sa.UniqueConstraint("cliente_id", name="uq_puertos_nap_cliente_id"),
    )
    op.create_index(
        "ix_puertos_nap_caja_estado",
        "puertos_nap",
        ["caja_nap_id", "estado"],
    )

    op.create_table(
        "historial_equipos",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("onu_id", sa.Integer(), nullable=False),
        sa.Column("cliente_id", sa.Integer(), nullable=True),
        sa.Column("tecnico_id", sa.Integer(), nullable=True),
        sa.Column("orden_id", sa.Integer(), nullable=True),
        sa.Column("tipo_movimiento", sa.String(30), nullable=False),
        sa.Column("estado_anterior", sa.String(50), nullable=True),
        sa.Column("estado_nuevo", sa.String(50), nullable=False),
        sa.Column("condicion", sa.String(30), nullable=True),
        sa.Column("motivo", sa.String(500), nullable=True),
        sa.Column("potencia_optica_dbm", sa.Numeric(6, 2), nullable=True),
        sa.Column(
            "fecha",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["onu_id"],
            ["inventario_onus.id"],
            name="fk_historial_equipo_onu",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["cliente_id"],
            ["clientes.id"],
            name="fk_historial_equipo_cliente",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["tecnico_id"],
            ["usuarios.id"],
            name="fk_historial_equipo_tecnico",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["orden_id"],
            ["ordenes_servicio.id"],
            name="fk_historial_equipo_orden",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_historial_equipo_fecha",
        "historial_equipos",
        ["onu_id", "fecha"],
    )

    op.create_table(
        "lecturas_opticas",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("cliente_id", sa.Integer(), nullable=False),
        sa.Column("onu_id", sa.Integer(), nullable=True),
        sa.Column("orden_id", sa.Integer(), nullable=True),
        sa.Column("tecnico_id", sa.Integer(), nullable=True),
        sa.Column("potencia_rx_dbm", sa.Numeric(6, 2), nullable=False),
        sa.Column("potencia_tx_dbm", sa.Numeric(6, 2), nullable=True),
        sa.Column("origen", sa.String(20), server_default="manual", nullable=False),
        sa.Column("observaciones", sa.String(500), nullable=True),
        sa.Column(
            "fecha",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["cliente_id"],
            ["clientes.id"],
            name="fk_lectura_cliente",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["onu_id"],
            ["inventario_onus.id"],
            name="fk_lectura_onu",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["orden_id"],
            ["ordenes_servicio.id"],
            name="fk_lectura_orden",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["tecnico_id"],
            ["usuarios.id"],
            name="fk_lectura_tecnico",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_lecturas_opticas_cliente_fecha",
        "lecturas_opticas",
        ["cliente_id", "fecha"],
    )
    op.create_index(
        "ix_lecturas_opticas_onu_fecha",
        "lecturas_opticas",
        ["onu_id", "fecha"],
    )

    op.create_unique_constraint(
        "uq_clientes_onu_id",
        "clientes",
        ["onu_id"],
    )
    op.create_unique_constraint(
        "uq_clientes_nap_puerto",
        "clientes",
        ["caja_nap_id", "puerto_nap"],
    )

    # Materializar todos los puertos y conservar ocupaciones existentes.
    naps = bind.execute(
        text("SELECT id, capacidad FROM cajas_nap")
    ).mappings().all()
    for nap in naps:
        for numero in range(1, int(nap["capacidad"] or 0) + 1):
            cliente = bind.execute(
                text("""
                    SELECT id
                    FROM clientes
                    WHERE caja_nap_id = :nap_id AND puerto_nap = :numero
                    LIMIT 1
                """),
                {"nap_id": nap["id"], "numero": numero},
            ).first()
            bind.execute(
                text("""
                    INSERT INTO puertos_nap
                        (caja_nap_id, numero, estado, cliente_id)
                    VALUES
                        (:nap_id, :numero, :estado, :cliente_id)
                """),
                {
                    "nap_id": nap["id"],
                    "numero": numero,
                    "estado": "ocupado" if cliente else "libre",
                    "cliente_id": cliente[0] if cliente else None,
                },
            )

    # Convertir instalaciones pendientes existentes en órdenes reales.
    bind.execute(text("""
        INSERT INTO ordenes_servicio (
            tipo,
            cliente_id,
            tecnico_id,
            prioridad,
            estado,
            motivo,
            descripcion,
            created_at,
            updated_at
        )
        SELECT
            'instalacion',
            c.id,
            c.tecnico_id,
            'normal',
            CASE WHEN c.tecnico_id IS NULL THEN 'pendiente' ELSE 'asignada' END,
            'Instalación pendiente',
            'Migrada automáticamente desde el flujo anterior',
            COALESCE(c.created_at, CURRENT_TIMESTAMP),
            CURRENT_TIMESTAMP
        FROM clientes c
        WHERE c.estado = 'pendiente_instalacion'
          AND NOT EXISTS (
              SELECT 1
              FROM ordenes_servicio o
              WHERE o.cliente_id = c.id
                AND o.tipo = 'instalacion'
                AND o.estado NOT IN ('terminada', 'cancelada')
          )
    """))
    bind.execute(text("""
        INSERT INTO historial_estados_orden (
            orden_id,
            usuario_id,
            estado_anterior,
            estado_nuevo,
            comentario
        )
        SELECT
            o.id,
            o.creado_por_id,
            NULL,
            o.estado,
            'Orden migrada desde instalación pendiente'
        FROM ordenes_servicio o
        WHERE o.descripcion = 'Migrada automáticamente desde el flujo anterior'
    """))

    bind.execute(text("""
        INSERT INTO historial_equipos (
            onu_id,
            cliente_id,
            tecnico_id,
            tipo_movimiento,
            estado_anterior,
            estado_nuevo,
            motivo
        )
        SELECT
            c.onu_id,
            c.id,
            c.tecnico_id,
            'registro_inicial',
            NULL,
            i.estado,
            'Asignación existente al habilitar trazabilidad'
        FROM clientes c
        JOIN inventario_onus i ON i.id = c.onu_id
        WHERE c.onu_id IS NOT NULL
    """))


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    unique_names = {
        item["name"]
        for item in inspector.get_unique_constraints("clientes")
    }
    if "uq_clientes_nap_puerto" in unique_names:
        op.drop_constraint(
            "uq_clientes_nap_puerto",
            "clientes",
            type_="unique",
        )
    if "uq_clientes_onu_id" in unique_names:
        op.drop_constraint(
            "uq_clientes_onu_id",
            "clientes",
            type_="unique",
        )

    for table_name in [
        "lecturas_opticas",
        "historial_equipos",
        "puertos_nap",
        "materiales_orden",
        "evidencias_orden",
        "historial_estados_orden",
        "ordenes_servicio",
    ]:
        if table_name in inspect(bind).get_table_names():
            op.drop_table(table_name)
