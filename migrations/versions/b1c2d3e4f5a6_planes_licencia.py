"""agrega planes, vigencia y limites de licencia

Revision ID: b1c2d3e4f5a6
Revises: a0b1c2d3e4f5
Create Date: 2026-09-09
"""

from alembic import op
import sqlalchemy as sa


revision = "b1c2d3e4f5a6"
down_revision = "a0b1c2d3e4f5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "planes_licencia",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("codigo", sa.String(50), nullable=False, unique=True),
        sa.Column("nombre", sa.String(120), nullable=False),
        sa.Column("tipo", sa.String(20), nullable=False),
        sa.Column("precio_mensual", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("duracion_dias", sa.Integer(), nullable=True),
        sa.Column("dias_gracia", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("limite_clientes", sa.Integer(), nullable=True),
        sa.Column("limite_routers", sa.Integer(), nullable=True),
        sa.Column("activo", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("creado_en", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_planes_licencia_codigo", "planes_licencia", ["codigo"], unique=True)
    planes = sa.table(
        "planes_licencia",
        sa.column("codigo", sa.String), sa.column("nombre", sa.String),
        sa.column("tipo", sa.String), sa.column("precio_mensual", sa.Numeric),
        sa.column("duracion_dias", sa.Integer), sa.column("dias_gracia", sa.Integer),
        sa.column("limite_clientes", sa.Integer), sa.column("limite_routers", sa.Integer),
        sa.column("activo", sa.Boolean),
    )
    op.bulk_insert(planes, [
        {"codigo": "demo", "nombre": "Demo 15 días", "tipo": "demo", "precio_mensual": 0, "duracion_dias": 15, "dias_gracia": 0, "limite_clientes": 50, "limite_routers": 1, "activo": True},
        {"codigo": "basico", "nombre": "Básico", "tipo": "mensual", "precio_mensual": 0, "duracion_dias": 30, "dias_gracia": 3, "limite_clientes": 300, "limite_routers": 2, "activo": True},
        {"codigo": "profesional", "nombre": "Profesional", "tipo": "mensual", "precio_mensual": 0, "duracion_dias": 30, "dias_gracia": 5, "limite_clientes": 1000, "limite_routers": 10, "activo": True},
        {"codigo": "ilimitado", "nombre": "Ilimitado", "tipo": "permanente", "precio_mensual": 0, "duracion_dias": None, "dias_gracia": 0, "limite_clientes": None, "limite_routers": None, "activo": True},
    ])
    for name, column in [
        ("plan_licencia_id", sa.Column("plan_licencia_id", sa.Integer(), nullable=True)),
        ("plan_nombre", sa.Column("plan_nombre", sa.String(120), nullable=True)),
        ("plan_tipo", sa.Column("plan_tipo", sa.String(20), nullable=True)),
        ("precio_mensual", sa.Column("precio_mensual", sa.Numeric(12, 2), nullable=True)),
        ("limite_clientes", sa.Column("limite_clientes", sa.Integer(), nullable=True)),
        ("limite_routers", sa.Column("limite_routers", sa.Integer(), nullable=True)),
        ("dias_gracia", sa.Column("dias_gracia", sa.Integer(), nullable=False, server_default="0")),
        ("suscripcion_inicio", sa.Column("suscripcion_inicio", sa.DateTime(), nullable=True)),
        ("suscripcion_vence", sa.Column("suscripcion_vence", sa.DateTime(), nullable=True)),
        ("uso_clientes", sa.Column("uso_clientes", sa.Integer(), nullable=False, server_default="0")),
        ("uso_routers", sa.Column("uso_routers", sa.Integer(), nullable=False, server_default="0")),
    ]:
        op.add_column("instalaciones_sistema", column)
    op.create_foreign_key("fk_instalacion_plan_licencia", "instalaciones_sistema", "planes_licencia", ["plan_licencia_id"], ["id"])
    op.create_table(
        "pagos_licencia",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("instalacion_id", sa.Integer(), nullable=False),
        sa.Column("plan_licencia_id", sa.Integer(), nullable=True),
        sa.Column("meses", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("monto", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("referencia", sa.String(160), nullable=True),
        sa.Column("registrado_en", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["instalacion_id"], ["instalaciones_sistema.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["plan_licencia_id"], ["planes_licencia.id"]),
    )
    op.create_index("ix_pagos_licencia_instalacion_id", "pagos_licencia", ["instalacion_id"])
    for name, column in [
        ("licencia_plan", sa.Column("licencia_plan", sa.String(120), nullable=True)),
        ("licencia_tipo", sa.Column("licencia_tipo", sa.String(20), nullable=True)),
        ("licencia_vigente_hasta", sa.Column("licencia_vigente_hasta", sa.DateTime(), nullable=True)),
        ("licencia_dias_gracia", sa.Column("licencia_dias_gracia", sa.Integer(), nullable=False, server_default="0")),
        ("licencia_limite_clientes", sa.Column("licencia_limite_clientes", sa.Integer(), nullable=True)),
        ("licencia_limite_routers", sa.Column("licencia_limite_routers", sa.Integer(), nullable=True)),
        ("licencia_uso_clientes", sa.Column("licencia_uso_clientes", sa.Integer(), nullable=False, server_default="0")),
        ("licencia_uso_routers", sa.Column("licencia_uso_routers", sa.Integer(), nullable=False, server_default="0")),
    ]:
        op.add_column("configuracion_sistema", column)


def downgrade() -> None:
    for column in ["licencia_uso_routers", "licencia_uso_clientes", "licencia_limite_routers", "licencia_limite_clientes", "licencia_dias_gracia", "licencia_vigente_hasta", "licencia_tipo", "licencia_plan"]:
        op.drop_column("configuracion_sistema", column)
    op.drop_index("ix_pagos_licencia_instalacion_id", table_name="pagos_licencia")
    op.drop_table("pagos_licencia")
    op.drop_constraint("fk_instalacion_plan_licencia", "instalaciones_sistema", type_="foreignkey")
    for column in ["uso_routers", "uso_clientes", "suscripcion_vence", "suscripcion_inicio", "dias_gracia", "limite_routers", "limite_clientes", "precio_mensual", "plan_tipo", "plan_nombre", "plan_licencia_id"]:
        op.drop_column("instalaciones_sistema", column)
    op.drop_index("ix_planes_licencia_codigo", table_name="planes_licencia")
    op.drop_table("planes_licencia")
