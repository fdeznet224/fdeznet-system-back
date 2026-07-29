"""finanzas operativas, caja y politicas de cobranza

Revision ID: a0c1d2e3f4a5
Revises: f9b0c1d2e3f4
Create Date: 2026-07-28
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


revision = "a0c1d2e3f4a5"
down_revision = "f9b0c1d2e3f4"
branch_labels = None
depends_on = None


def _normalizar_importes(bind):
    bind.execute(text("UPDATE planes SET precio = 0 WHERE precio IS NULL"))
    bind.execute(text(
        "UPDATE plantillas_facturacion SET impuesto = 0 WHERE impuesto IS NULL"
    ))
    bind.execute(text(
        "UPDATE clientes SET saldo_a_favor = 0 WHERE saldo_a_favor IS NULL"
    ))
    bind.execute(text("""
        UPDATE facturas
        SET monto = COALESCE(monto, 0),
            impuesto = COALESCE(impuesto, 0),
            total = COALESCE(total, 0),
            saldo_pendiente = COALESCE(saldo_pendiente, 0)
    """))
    bind.execute(text(
        "UPDATE pagos SET monto_total = 0 WHERE monto_total IS NULL"
    ))


def upgrade():
    bind = op.get_bind()
    _normalizar_importes(bind)

    op.alter_column(
        "planes",
        "precio",
        existing_type=sa.Float(),
        type_=sa.Numeric(12, 2),
        nullable=False,
    )
    op.alter_column(
        "plantillas_facturacion",
        "impuesto",
        existing_type=sa.Float(),
        type_=sa.Numeric(5, 2),
        existing_nullable=True,
        server_default="0",
    )
    op.alter_column(
        "clientes",
        "saldo_a_favor",
        existing_type=sa.Float(),
        type_=sa.Numeric(12, 2),
        nullable=False,
        server_default="0",
    )
    for column in ("monto", "impuesto", "total", "saldo_pendiente"):
        op.alter_column(
            "facturas",
            column,
            existing_type=sa.Float(),
            type_=sa.Numeric(12, 2),
            nullable=False,
            server_default="0" if column in {"impuesto", "saldo_pendiente"} else None,
        )
    op.alter_column(
        "facturas",
        "precio_mensual_snapshot",
        existing_type=sa.Float(),
        type_=sa.Numeric(12, 2),
        existing_nullable=True,
    )
    op.alter_column(
        "facturas",
        "precio_diario",
        existing_type=sa.Float(),
        type_=sa.Numeric(12, 4),
        existing_nullable=True,
    )
    op.alter_column(
        "pagos",
        "monto_total",
        existing_type=sa.Float(),
        type_=sa.Numeric(12, 2),
        nullable=False,
    )
    op.alter_column(
        "pagos_autovalidados",
        "monto",
        existing_type=sa.Float(),
        type_=sa.Numeric(12, 2),
        nullable=False,
    )

    op.create_table(
        "politicas_cobranza",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("nombre", sa.String(100), nullable=False),
        sa.Column("tipo_cliente", sa.String(30), nullable=False),
        sa.Column("dias_max_promesa", sa.Integer(), server_default="7", nullable=False),
        sa.Column("max_promesas_activas", sa.Integer(), server_default="1", nullable=False),
        sa.Column("max_incumplidas_90_dias", sa.Integer(), server_default="2", nullable=False),
        sa.Column("permite_reconexion", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("activa", sa.Boolean(), server_default="1", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.UniqueConstraint("nombre", name="uq_politicas_cobranza_nombre"),
        sa.UniqueConstraint("tipo_cliente", name="uq_politicas_cobranza_tipo"),
    )
    bind.execute(text("""
        INSERT INTO politicas_cobranza (
            nombre, tipo_cliente, dias_max_promesa,
            max_promesas_activas, max_incumplidas_90_dias,
            permite_reconexion, activa
        ) VALUES (
            'Residencial', 'residencial', 7, 1, 2, 1, 1
        )
    """))
    op.add_column(
        "clientes",
        sa.Column(
            "tipo_cliente",
            sa.String(30),
            server_default="residencial",
            nullable=False,
        ),
    )
    op.add_column(
        "clientes",
        sa.Column("politica_cobranza_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_clientes_politica_cobranza_id",
        "clientes",
        ["politica_cobranza_id"],
    )
    op.create_foreign_key(
        "fk_cliente_politica_cobranza",
        "clientes",
        "politicas_cobranza",
        ["politica_cobranza_id"],
        ["id"],
    )
    bind.execute(text("""
        UPDATE clientes
        SET politica_cobranza_id = (
            SELECT id FROM politicas_cobranza
            WHERE tipo_cliente = 'residencial'
            LIMIT 1
        )
        WHERE politica_cobranza_id IS NULL
    """))

    op.add_column(
        "facturas",
        sa.Column(
            "descuento_total",
            sa.Numeric(12, 2),
            server_default="0",
            nullable=False,
        ),
    )

    op.create_table(
        "cajas_sesion",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("usuario_id", sa.Integer(), nullable=False),
        sa.Column("estado", sa.String(20), server_default="abierta", nullable=False),
        sa.Column("monto_apertura", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("monto_esperado", sa.Numeric(12, 2), nullable=True),
        sa.Column("monto_entregado", sa.Numeric(12, 2), nullable=True),
        sa.Column("diferencia", sa.Numeric(12, 2), nullable=True),
        sa.Column("notas_apertura", sa.String(500), nullable=True),
        sa.Column("notas_cierre", sa.String(500), nullable=True),
        sa.Column(
            "abierta_en",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("cerrada_en", sa.DateTime(), nullable=True),
        sa.Column("cerrada_por_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["usuario_id"], ["usuarios.id"], name="fk_caja_usuario"),
        sa.ForeignKeyConstraint(
            ["cerrada_por_id"],
            ["usuarios.id"],
            name="fk_caja_cerrada_por",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_cajas_sesion_usuario_id",
        "cajas_sesion",
        ["usuario_id"],
    )
    op.create_index(
        "ix_cajas_usuario_estado",
        "cajas_sesion",
        ["usuario_id", "estado"],
    )

    for name, column in (
        ("caja_sesion_id", sa.Column("caja_sesion_id", sa.Integer(), nullable=True)),
        ("monto_aplicado", sa.Column("monto_aplicado", sa.Numeric(12, 2), server_default="0", nullable=False)),
        ("monto_saldo_favor", sa.Column("monto_saldo_favor", sa.Numeric(12, 2), server_default="0", nullable=False)),
        ("monto_saldo_favor_usado", sa.Column("monto_saldo_favor_usado", sa.Numeric(12, 2), server_default="0", nullable=False)),
        ("saldo_anterior", sa.Column("saldo_anterior", sa.Numeric(12, 2), nullable=True)),
        ("saldo_posterior", sa.Column("saldo_posterior", sa.Numeric(12, 2), nullable=True)),
        ("clave_idempotencia", sa.Column("clave_idempotencia", sa.String(100), nullable=True)),
        ("estado", sa.Column("estado", sa.String(20), server_default="aplicado", nullable=False)),
        ("motivo_anulacion", sa.Column("motivo_anulacion", sa.String(500), nullable=True)),
        ("anulado_por_id", sa.Column("anulado_por_id", sa.Integer(), nullable=True)),
        ("fecha_anulacion", sa.Column("fecha_anulacion", sa.DateTime(), nullable=True)),
    ):
        op.add_column("pagos", column)
    op.create_index("ix_pagos_caja_sesion_id", "pagos", ["caja_sesion_id"])
    op.create_unique_constraint(
        "uq_pagos_clave_idempotencia",
        "pagos",
        ["clave_idempotencia"],
    )
    op.create_foreign_key(
        "fk_pago_caja",
        "pagos",
        "cajas_sesion",
        ["caja_sesion_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_pago_anulado_por",
        "pagos",
        "usuarios",
        ["anulado_por_id"],
        ["id"],
        ondelete="SET NULL",
    )
    bind.execute(text("""
        UPDATE pagos
        SET monto_aplicado = monto_total,
            monto_saldo_favor = 0,
            monto_saldo_favor_usado = 0,
            estado = 'aplicado'
    """))

    op.create_table(
        "movimientos_caja",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("caja_sesion_id", sa.Integer(), nullable=False),
        sa.Column("usuario_id", sa.Integer(), nullable=False),
        sa.Column("pago_id", sa.Integer(), nullable=True),
        sa.Column("tipo", sa.String(20), nullable=False),
        sa.Column("categoria", sa.String(50), nullable=False),
        sa.Column("metodo_pago", sa.String(50), server_default="efectivo", nullable=False),
        sa.Column("monto", sa.Numeric(12, 2), nullable=False),
        sa.Column("referencia", sa.String(100), nullable=True),
        sa.Column("descripcion", sa.String(500), nullable=False),
        sa.Column("estado", sa.String(20), server_default="aplicado", nullable=False),
        sa.Column("motivo_anulacion", sa.String(500), nullable=True),
        sa.Column("anulado_por_id", sa.Integer(), nullable=True),
        sa.Column("fecha_anulacion", sa.DateTime(), nullable=True),
        sa.Column(
            "fecha",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["caja_sesion_id"],
            ["cajas_sesion.id"],
            name="fk_movimiento_caja",
        ),
        sa.ForeignKeyConstraint(["usuario_id"], ["usuarios.id"], name="fk_movimiento_usuario"),
        sa.ForeignKeyConstraint(
            ["pago_id"],
            ["pagos.id"],
            name="fk_movimiento_pago",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["anulado_por_id"],
            ["usuarios.id"],
            name="fk_movimiento_anulado_por",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("pago_id", name="uq_movimiento_pago"),
    )
    op.create_index(
        "ix_movimientos_caja_sesion_id",
        "movimientos_caja",
        ["caja_sesion_id"],
    )
    op.create_index(
        "ix_movimientos_caja_fecha",
        "movimientos_caja",
        ["caja_sesion_id", "fecha"],
    )

    op.create_table(
        "descuentos_factura",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("factura_id", sa.Integer(), nullable=False),
        sa.Column("aplicado_por_id", sa.Integer(), nullable=True),
        sa.Column("autorizado_por_id", sa.Integer(), nullable=True),
        sa.Column("monto", sa.Numeric(12, 2), nullable=False),
        sa.Column("saldo_anterior", sa.Numeric(12, 2), nullable=False),
        sa.Column("saldo_posterior", sa.Numeric(12, 2), nullable=False),
        sa.Column("motivo", sa.String(500), nullable=False),
        sa.Column(
            "fecha",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["factura_id"], ["facturas.id"], name="fk_descuento_factura"),
        sa.ForeignKeyConstraint(
            ["aplicado_por_id"],
            ["usuarios.id"],
            name="fk_descuento_aplicado_por",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["autorizado_por_id"],
            ["usuarios.id"],
            name="fk_descuento_autorizado_por",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_descuentos_factura_factura_id",
        "descuentos_factura",
        ["factura_id"],
    )

    op.create_table(
        "promesas_pago_historial",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("factura_id", sa.Integer(), nullable=False),
        sa.Column("cliente_id", sa.Integer(), nullable=False),
        sa.Column("usuario_id", sa.Integer(), nullable=True),
        sa.Column("fecha_prometida", sa.Date(), nullable=False),
        sa.Column("fecha_anterior", sa.Date(), nullable=True),
        sa.Column("estado", sa.String(20), server_default="activa", nullable=False),
        sa.Column("notas", sa.String(500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("resuelta_en", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["factura_id"], ["facturas.id"], name="fk_promesa_factura"),
        sa.ForeignKeyConstraint(["cliente_id"], ["clientes.id"], name="fk_promesa_cliente"),
        sa.ForeignKeyConstraint(
            ["usuario_id"],
            ["usuarios.id"],
            name="fk_promesa_usuario",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_promesas_pago_historial_factura_id",
        "promesas_pago_historial",
        ["factura_id"],
    )
    op.create_index(
        "ix_promesas_pago_historial_cliente_id",
        "promesas_pago_historial",
        ["cliente_id"],
    )
    op.create_index(
        "ix_promesas_cliente_estado",
        "promesas_pago_historial",
        ["cliente_id", "estado"],
    )
    bind.execute(text("""
        INSERT INTO promesas_pago_historial (
            factura_id, cliente_id, fecha_prometida, estado, notas
        )
        SELECT id, cliente_id, fecha_promesa_pago, 'activa',
               'Promesa activa migrada desde el flujo anterior'
        FROM facturas
        WHERE es_promesa_activa = 1
          AND fecha_promesa_pago IS NOT NULL
    """))

    op.add_column(
        "mensajes_chat",
        sa.Column("tipo_evento", sa.String(50), nullable=True),
    )
    op.add_column(
        "mensajes_chat",
        sa.Column("clave_dedupe", sa.String(150), nullable=True),
    )
    op.create_unique_constraint(
        "uq_mensajes_chat_clave_dedupe",
        "mensajes_chat",
        ["clave_dedupe"],
    )


def downgrade():
    op.drop_constraint(
        "uq_mensajes_chat_clave_dedupe",
        "mensajes_chat",
        type_="unique",
    )
    op.drop_column("mensajes_chat", "clave_dedupe")
    op.drop_column("mensajes_chat", "tipo_evento")
    op.drop_table("promesas_pago_historial")
    op.drop_table("descuentos_factura")
    op.drop_table("movimientos_caja")

    op.drop_constraint("fk_pago_anulado_por", "pagos", type_="foreignkey")
    op.drop_constraint("fk_pago_caja", "pagos", type_="foreignkey")
    op.drop_constraint("uq_pagos_clave_idempotencia", "pagos", type_="unique")
    op.drop_index("ix_pagos_caja_sesion_id", table_name="pagos")
    for column in (
        "fecha_anulacion",
        "anulado_por_id",
        "motivo_anulacion",
        "estado",
        "clave_idempotencia",
        "saldo_posterior",
        "saldo_anterior",
        "monto_saldo_favor",
        "monto_saldo_favor_usado",
        "monto_aplicado",
        "caja_sesion_id",
    ):
        op.drop_column("pagos", column)

    op.drop_table("cajas_sesion")
    op.drop_column("facturas", "descuento_total")
    op.drop_constraint(
        "fk_cliente_politica_cobranza",
        "clientes",
        type_="foreignkey",
    )
    op.drop_index("ix_clientes_politica_cobranza_id", table_name="clientes")
    op.drop_column("clientes", "politica_cobranza_id")
    op.drop_column("clientes", "tipo_cliente")
    op.drop_table("politicas_cobranza")

    op.alter_column(
        "pagos_autovalidados",
        "monto",
        existing_type=sa.Numeric(12, 2),
        type_=sa.Float(),
        nullable=False,
    )
    op.alter_column(
        "pagos",
        "monto_total",
        existing_type=sa.Numeric(12, 2),
        type_=sa.Float(),
        existing_nullable=False,
    )
    op.alter_column(
        "facturas",
        "precio_diario",
        existing_type=sa.Numeric(12, 4),
        type_=sa.Float(),
        existing_nullable=True,
    )
    op.alter_column(
        "facturas",
        "precio_mensual_snapshot",
        existing_type=sa.Numeric(12, 2),
        type_=sa.Float(),
        existing_nullable=True,
    )
    for column in ("saldo_pendiente", "total", "impuesto", "monto"):
        op.alter_column(
            "facturas",
            column,
            existing_type=sa.Numeric(12, 2),
            type_=sa.Float(),
            nullable=True,
            server_default=None,
        )
    op.alter_column(
        "clientes",
        "saldo_a_favor",
        existing_type=sa.Numeric(12, 2),
        type_=sa.Float(),
        nullable=True,
        server_default=None,
    )
    op.alter_column(
        "plantillas_facturacion",
        "impuesto",
        existing_type=sa.Numeric(5, 2),
        type_=sa.Float(),
        existing_nullable=True,
        server_default=None,
    )
    op.alter_column(
        "planes",
        "precio",
        existing_type=sa.Numeric(12, 2),
        type_=sa.Float(),
        nullable=True,
    )
