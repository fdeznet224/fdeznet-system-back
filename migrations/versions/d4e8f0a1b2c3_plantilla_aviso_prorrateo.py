"""Plantilla del aviso de prorrateo (no corta; se suma a la próxima factura)

La deja en Plantillas Mensajes para que el ISP la edite; si ya existe, no se toca.

Revision ID: d4e8f0a1b2c3
Revises: c3d9e1f2a7b4
Create Date: 2026-11-03
"""

from alembic import op
import sqlalchemy as sa


revision = "d4e8f0a1b2c3"
down_revision = "c3d9e1f2a7b4"
branch_labels = None
depends_on = None

AVISO_PRORRATEO = (
    "🧾 Hola {nombre}, tienes un saldo de prorrateo de *{total_factura}* "
    "por los días del *{periodo_desde} al {periodo_hasta}* "
    "({dias_con_servicio} días), para que tu pago quede siempre el mismo día.\n\n"
    "Este saldo *no corta tu servicio*, pero te recomendamos pagarlo: "
    "se incluirá en tu próxima factura del *{fecha_proxima_factura}*.\n\n"
    "Si ya pagaste, omite este mensaje."
)


def upgrade() -> None:
    op.get_bind().execute(
        sa.text(
            "INSERT INTO plantillas_mensajes (tipo, texto, activo) "
            "SELECT 'aviso_prorrateo', :texto, 1 FROM DUAL "
            "WHERE NOT EXISTS (SELECT 1 FROM plantillas_mensajes WHERE tipo = 'aviso_prorrateo')"
        ),
        {"texto": AVISO_PRORRATEO},
    )


def downgrade() -> None:
    op.get_bind().execute(sa.text("DELETE FROM plantillas_mensajes WHERE tipo = 'aviso_prorrateo'"))
