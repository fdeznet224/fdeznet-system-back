"""flujos visuales y acceso seguro al bot tecnico

Revision ID: b7c8d9e0f1a2
Revises: a6b7c8d9e0f1
"""

import json

from alembic import op
import sqlalchemy as sa


revision = "b7c8d9e0f1a2"
down_revision = "a6b7c8d9e0f1"
branch_labels = None
depends_on = None


PUBLIC_NODES = [
    {"id": "start", "type": "trigger", "title": "Inicio", "text": "", "x": 60, "y": 220},
    {"id": "welcome", "type": "message", "title": "Bienvenida", "text": "Hola, soy tu asistente. ¿En qué puedo ayudarte?", "x": 300, "y": 220},
    {"id": "menu", "type": "menu", "title": "Menú principal", "text": "Elige una opción:", "x": 560, "y": 220},
    {"id": "pay", "type": "action", "title": "Reportar pago", "action": "reportar_pago", "text": "", "x": 850, "y": 20},
    {"id": "promise", "type": "action", "title": "Promesa de pago", "action": "promesa_pago", "text": "", "x": 850, "y": 130},
    {"id": "status", "type": "action", "title": "Servicio y saldo", "action": "estado_servicio", "text": "", "x": 850, "y": 240},
    {"id": "bank", "type": "action", "title": "Datos de pago", "action": "datos_pago", "text": "", "x": 850, "y": 350},
    {"id": "support", "type": "action", "title": "Diagnóstico", "action": "diagnostico_tecnico", "text": "", "x": 850, "y": 460},
]
PUBLIC_EDGES = [
    {"id": "e1", "source": "start", "target": "welcome", "label": ""},
    {"id": "e2", "source": "welcome", "target": "menu", "label": ""},
    {"id": "e3", "source": "menu", "target": "pay", "label": "Reportar pago"},
    {"id": "e4", "source": "menu", "target": "promise", "label": "Promesa de pago"},
    {"id": "e5", "source": "menu", "target": "status", "label": "Servicio y saldo"},
    {"id": "e6", "source": "menu", "target": "bank", "label": "Datos de pago"},
    {"id": "e7", "source": "menu", "target": "support", "label": "No tengo internet"},
]
TECH_NODES = [
    {"id": "start", "type": "trigger", "title": "Inicio técnico", "text": "", "x": 60, "y": 180},
    {"id": "welcome", "type": "message", "title": "Acceso privado", "text": "Panel técnico autorizado. Elige una herramienta:", "x": 300, "y": 180},
    {"id": "menu", "type": "menu", "title": "Herramientas", "text": "¿Qué deseas consultar?", "x": 580, "y": 180},
    {"id": "pppoe", "type": "action", "title": "Verificar PPPoE", "action": "tecnico_pppoe", "text": "", "x": 880, "y": 20},
    {"id": "optical", "type": "action", "title": "Verificar potencia", "action": "tecnico_potencia", "text": "", "x": 880, "y": 150},
    {"id": "network", "type": "action", "title": "Ficha de red", "action": "tecnico_red", "text": "", "x": 880, "y": 280},
    {"id": "diag", "type": "action", "title": "Diagnóstico completo", "action": "tecnico_diagnostico", "text": "", "x": 880, "y": 410},
]
TECH_EDGES = [
    {"id": "e1", "source": "start", "target": "welcome", "label": ""},
    {"id": "e2", "source": "welcome", "target": "menu", "label": ""},
    {"id": "e3", "source": "menu", "target": "pppoe", "label": "Verificar sesión PPPoE"},
    {"id": "e4", "source": "menu", "target": "optical", "label": "Verificar ONU y potencia óptica"},
    {"id": "e5", "source": "menu", "target": "network", "label": "NAP, puerto y datos de red"},
    {"id": "e6", "source": "menu", "target": "diag", "label": "Diagnóstico completo"},
]


def upgrade() -> None:
    op.add_column("usuarios", sa.Column("telefono_whatsapp", sa.String(20), nullable=True))
    op.add_column(
        "usuarios",
        sa.Column("bot_whatsapp_habilitado", sa.Boolean(), nullable=False, server_default="0"),
    )
    op.create_unique_constraint(
        "uq_usuarios_telefono_whatsapp",
        "usuarios",
        ["telefono_whatsapp"],
    )
    table = op.create_table(
        "flujos_bot",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("alcance", sa.String(20), nullable=False),
        sa.Column("nombre", sa.String(100), nullable=False),
        sa.Column("activo", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("comando", sa.String(30), nullable=False),
        sa.Column("nodos_json", sa.Text(), nullable=False),
        sa.Column("conexiones_json", sa.Text(), nullable=False),
        sa.Column("actualizado_en", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("alcance", name="uq_flujos_bot_alcance"),
    )
    op.bulk_insert(table, [
        {
            "id": 1,
            "alcance": "cliente",
            "nombre": "Autoservicio de clientes",
            "activo": True,
            "comando": "fdezbot",
            "nodos_json": json.dumps(PUBLIC_NODES, ensure_ascii=False),
            "conexiones_json": json.dumps(PUBLIC_EDGES, ensure_ascii=False),
        },
        {
            "id": 2,
            "alcance": "tecnico",
            "nombre": "Herramientas para técnicos",
            "activo": True,
            "comando": "tecnico",
            "nodos_json": json.dumps(TECH_NODES, ensure_ascii=False),
            "conexiones_json": json.dumps(TECH_EDGES, ensure_ascii=False),
        },
    ])


def downgrade() -> None:
    op.drop_table("flujos_bot")
    op.drop_constraint("uq_usuarios_telefono_whatsapp", "usuarios", type_="unique")
    op.drop_column("usuarios", "bot_whatsapp_habilitado")
    op.drop_column("usuarios", "telefono_whatsapp")
