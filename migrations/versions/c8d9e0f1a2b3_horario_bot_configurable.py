"""horario y mensaje fuera de horario configurables

Revision ID: c8d9e0f1a2b3
Revises: b7c8d9e0f1a2
"""

import json

from alembic import op
import sqlalchemy as sa

revision = "c8d9e0f1a2b3"
down_revision = "b7c8d9e0f1a2"
branch_labels = None
depends_on = None

DEFAULT_SCHEDULE = {
    "lunes": {"activo": True, "inicio": "08:00", "fin": "20:00"},
    "martes": {"activo": True, "inicio": "08:00", "fin": "20:00"},
    "miercoles": {"activo": True, "inicio": "08:00", "fin": "20:00"},
    "jueves": {"activo": True, "inicio": "08:00", "fin": "20:00"},
    "viernes": {"activo": True, "inicio": "08:00", "fin": "20:00"},
    "sabado": {"activo": True, "inicio": "09:00", "fin": "14:00"},
    "domingo": {"activo": False, "inicio": "08:00", "fin": "20:00"},
}
DEFAULT_AWAY_MESSAGE = (
    "🌙 *Estamos fuera del horario de atención.*\n\n"
    "*Horario de atención:*\n{horario}\n\n"
    "🤖 Soy *{asistente}*, el asistente automático de {empresa}. "
    "Mientras regresamos puedo ayudarte con pagos, saldo, promesas y conexión."
)


def upgrade() -> None:
    op.add_column(
        "configuracion_bot",
        sa.Column(
            "zona_horaria",
            sa.String(64),
            nullable=False,
            server_default="America/Mexico_City",
        ),
    )
    op.add_column(
        "configuracion_bot",
        sa.Column(
            "horario_atencion_json",
            sa.Text(),
            nullable=True,
        ),
    )
    op.add_column(
        "configuracion_bot",
        sa.Column(
            "mensaje_fuera_horario",
            sa.Text(),
            nullable=True,
        ),
    )
    bot_config = sa.table(
        "configuracion_bot",
        sa.column("horario_atencion_json", sa.Text()),
        sa.column("mensaje_fuera_horario", sa.Text()),
    )
    op.execute(
        bot_config.update().values(
            horario_atencion_json=json.dumps(
                DEFAULT_SCHEDULE,
                ensure_ascii=False,
            ),
            mensaje_fuera_horario=DEFAULT_AWAY_MESSAGE,
        )
    )
    op.alter_column(
        "configuracion_bot",
        "horario_atencion_json",
        existing_type=sa.Text(),
        nullable=False,
    )
    op.alter_column(
        "configuracion_bot",
        "mensaje_fuera_horario",
        existing_type=sa.Text(),
        nullable=False,
    )


def downgrade() -> None:
    op.drop_column("configuracion_bot", "mensaje_fuera_horario")
    op.drop_column("configuracion_bot", "horario_atencion_json")
    op.drop_column("configuracion_bot", "zona_horaria")
