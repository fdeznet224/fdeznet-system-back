"""flujo configurable del bot y plantilla de datos de pago

Revision ID: a6b7c8d9e0f1
Revises: d4f5a6b7c8d9
"""

import json

from alembic import op
import sqlalchemy as sa


revision = "a6b7c8d9e0f1"
down_revision = "d4f5a6b7c8d9"
branch_labels = None
depends_on = None


DEFAULT_OPTIONS = [
    {"id": "reportar_pago", "label": "Reportar pago", "enabled": True},
    {"id": "promesa_pago", "label": "Promesa de pago", "enabled": True},
    {"id": "estado_servicio", "label": "Consultar mi servicio y saldo", "enabled": True},
    {"id": "datos_pago", "label": "Datos para depósito o transferencia", "enabled": True},
    {"id": "diagnostico_tecnico", "label": "No tengo internet / diagnóstico técnico", "enabled": True},
]


def upgrade() -> None:
    table = op.create_table(
        "configuracion_bot",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("activo", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("palabra_activacion", sa.String(30), nullable=False, server_default="fdezbot"),
        sa.Column("minutos_sesion", sa.Integer(), nullable=False, server_default="15"),
        sa.Column("inicio_fuera_horario", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("mensaje_bienvenida", sa.String(500), nullable=False, server_default="Soy tu asistente de pagos y servicios. Elige una opción:"),
        sa.Column("mensaje_despedida", sa.String(300), nullable=False, server_default="Asistente desactivado. Un asesor humano te atenderá a la brevedad."),
        sa.Column("opciones_json", sa.Text(), nullable=False),
        sa.Column("actualizado_en", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.bulk_insert(table, [{
        "id": 1,
        "activo": True,
        "palabra_activacion": "fdezbot",
        "minutos_sesion": 15,
        "inicio_fuera_horario": True,
        "mensaje_bienvenida": "Soy tu asistente de pagos y servicios. Elige una opción:",
        "mensaje_despedida": "Asistente desactivado. Un asesor humano te atenderá a la brevedad.",
        "opciones_json": json.dumps(DEFAULT_OPTIONS, ensure_ascii=False),
    }])
    op.execute(
        sa.text(
            "INSERT IGNORE INTO plantillas_mensajes (tipo, texto, activo) "
            "VALUES ('datos_pago', 'Configura aquí banco, beneficiario, cuenta o CLABE y referencia de pago.', 0)"
        )
    )


def downgrade() -> None:
    op.execute("DELETE FROM plantillas_mensajes WHERE tipo = 'datos_pago' AND activo = 0 AND texto LIKE 'Configura aquí banco,%'")
    op.drop_table("configuracion_bot")
