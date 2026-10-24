"""Bienvenida corta con el WiFi de la instalación

Solo cambia la plantilla larga que traía el sistema (la reconoce por su texto);
si el ISP ya escribió la suya, no se toca.

Revision ID: bb433f392313
Revises: e3f4a5b6c7d8
Create Date: 2026-10-23
"""

from alembic import op
import sqlalchemy as sa


revision = "bb433f392313"
down_revision = "e3f4a5b6c7d8"
branch_labels = None
depends_on = None

BIENVENIDA = (
    "👋 ¡Bienvenido a {empresa}, *{nombre}*!\n\n"
    "📄 Contrato: *{contrato}*\n"
    "📡 Plan: *{plan}* · {precio} al mes\n"
    "🗓️ Pagas del día *{dia_inicio_pago}* al *{ultimo_dia_pago}*; el corte es el día *{dia_corte_servicio}*.\n\n"
    "📶 Red WiFi: *{red_wifi}*\n"
    "🔑 Contraseña: *{clave_wifi}*\n\n"
    "📲 Tu portal: {portal} (entra con tu contrato)\n"
    "¿Dudas o fallas? Escríbenos aquí."
)


def upgrade() -> None:
    op.get_bind().execute(
        sa.text("UPDATE plantillas_mensajes SET texto = :texto WHERE tipo = 'bienvenida' AND texto LIKE :anterior"),
        {"texto": BIENVENIDA, "anterior": "%Guarda muy bien tus datos de cliente%"},
    )


def downgrade() -> None:
    pass
