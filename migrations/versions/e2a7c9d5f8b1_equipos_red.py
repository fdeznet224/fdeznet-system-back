"""Equipos de red (AP, PtP, switch) y el AP al que está conectado cada cliente de radio.

Revision ID: e2a7c9d5f8b1
Revises: d1f6b8c4e5a7
"""
import sqlalchemy as sa
from alembic import op

revision = "e2a7c9d5f8b1"
down_revision = "d1f6b8c4e5a7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "equipos_red",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("nombre", sa.String(100), nullable=False),
        sa.Column("tipo", sa.String(20), nullable=False, server_default="ap"),
        sa.Column("marca", sa.String(20), nullable=False, server_default="ubiquiti"),
        sa.Column("modelo", sa.String(100), nullable=True),
        sa.Column("ip", sa.String(45), nullable=False),
        sa.Column("router_id", sa.Integer(), sa.ForeignKey("routers.id", ondelete="SET NULL"), nullable=True, index=True),
        sa.Column("padre_id", sa.Integer(), sa.ForeignKey("equipos_red.id", ondelete="SET NULL"), nullable=True, index=True),
        sa.Column("zona_id", sa.Integer(), sa.ForeignKey("zonas.id", ondelete="SET NULL"), nullable=True),
        sa.Column("ubicacion", sa.String(200), nullable=True),
        sa.Column("latitud", sa.Float(), nullable=True),
        sa.Column("longitud", sa.Float(), nullable=True),
        sa.Column("usuario", sa.String(60), nullable=True),
        sa.Column("clave", sa.Text(), nullable=True),
        sa.Column("puerto_web", sa.String(6), nullable=False, server_default="443"),
        sa.Column("activo", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("responde", sa.Boolean(), nullable=True),
        sa.Column("latencia_ms", sa.Float(), nullable=True),
        sa.Column("perdida", sa.Integer(), nullable=True),
        sa.Column("revisado_en", sa.DateTime(), nullable=True),
        sa.Column("senal", sa.Float(), nullable=True),
        sa.Column("ruido", sa.Float(), nullable=True),
        sa.Column("ccq", sa.Float(), nullable=True),
        sa.Column("capacidad_tx", sa.Float(), nullable=True),
        sa.Column("capacidad_rx", sa.Float(), nullable=True),
        sa.Column("frecuencia", sa.String(30), nullable=True),
        sa.Column("ancho_canal", sa.String(20), nullable=True),
        sa.Column("ssid", sa.String(100), nullable=True),
        sa.Column("modo", sa.String(30), nullable=True),
        sa.Column("estaciones", sa.Integer(), nullable=True),
        sa.Column("firmware", sa.String(60), nullable=True),
        sa.Column("uptime_s", sa.Integer(), nullable=True),
        sa.Column("airos_leido_en", sa.DateTime(), nullable=True),
        sa.Column("airos_error", sa.String(300), nullable=True),
        sa.Column("creado_en", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    with op.batch_alter_table("clientes") as tabla:
        tabla.add_column(sa.Column("equipo_red_id", sa.Integer(), nullable=True))
        tabla.add_column(sa.Column("antena_senal", sa.Float(), nullable=True))
        tabla.create_index("ix_clientes_equipo_red_id", ["equipo_red_id"])
        tabla.create_foreign_key("fk_clientes_equipo_red", "equipos_red", ["equipo_red_id"], ["id"], ondelete="SET NULL")


def downgrade():
    with op.batch_alter_table("clientes") as tabla:
        tabla.drop_constraint("fk_clientes_equipo_red", type_="foreignkey")
        tabla.drop_index("ix_clientes_equipo_red_id")
        tabla.drop_column("antena_senal")
        tabla.drop_column("equipo_red_id")
    op.drop_table("equipos_red")
