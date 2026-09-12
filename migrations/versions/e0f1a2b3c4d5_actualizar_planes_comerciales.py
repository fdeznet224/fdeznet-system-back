"""actualiza planes comerciales en dolares

Revision ID: e0f1a2b3c4d5
Revises: d9e0f1a2b3c4
Create Date: 2026-09-11
"""

from alembic import op


revision = "e0f1a2b3c4d5"
down_revision = "d9e0f1a2b3c4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    plans = {
        "demo": ("Demo 7 días", "demo", 0, 7, 0, 30, None),
        "basico": ("Básico 200", "mensual", 25, 30, 3, 200, None),
        "profesional": ("Crecimiento 800", "mensual", 55, 30, 3, 800, None),
        "ilimitado": ("Ilimitado", "mensual", 85, 30, 3, None, None),
    }
    for code, (name, plan_type, price, duration, grace, clients, routers) in plans.items():
        op.execute(
            "UPDATE planes_licencia SET "
            f"nombre='{name}', tipo='{plan_type}', precio_mensual={price}, "
            f"duracion_dias={duration}, dias_gracia={grace}, "
            f"limite_clientes={'NULL' if clients is None else clients}, "
            f"limite_routers={'NULL' if routers is None else routers}, activo=1 "
            f"WHERE codigo='{code}'"
        )
    op.execute(
        "UPDATE instalaciones_sistema i "
        "JOIN planes_licencia p ON p.id=i.plan_licencia_id "
        "SET i.plan_nombre=p.nombre, i.plan_tipo=p.tipo, "
        "i.precio_mensual=p.precio_mensual, i.limite_clientes=p.limite_clientes, "
        "i.limite_routers=p.limite_routers, i.dias_gracia=p.dias_gracia"
    )


def downgrade() -> None:
    defaults = {
        "demo": ("Demo 15 días", "demo", 0, 15, 0, 50, 1),
        "basico": ("Básico", "mensual", 0, 30, 3, 300, 2),
        "profesional": ("Profesional", "mensual", 0, 30, 5, 1000, 10),
        "ilimitado": ("Ilimitado", "permanente", 0, None, 0, None, None),
    }
    for code, (name, plan_type, price, duration, grace, clients, routers) in defaults.items():
        op.execute(
            "UPDATE planes_licencia SET "
            f"nombre='{name}', tipo='{plan_type}', precio_mensual={price}, "
            f"duracion_dias={'NULL' if duration is None else duration}, "
            f"dias_gracia={grace}, "
            f"limite_clientes={'NULL' if clients is None else clients}, "
            f"limite_routers={'NULL' if routers is None else routers} "
            f"WHERE codigo='{code}'"
        )
