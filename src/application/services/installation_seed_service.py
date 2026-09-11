import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.services.bot_flow_service import (
    DEFAULT_AWAY_MESSAGE,
    DEFAULT_OPTIONS,
    DEFAULT_SCHEDULE,
)
from src.infrastructure.models import (
    ConfiguracionBotModel,
    FlujoBotModel,
    PlanLicenciaModel,
    PlantillaMensajeModel,
)


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
    {"id": "e5", "source": "menu", "target": "status", "label": "Ver servicio y saldo"},
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


async def seed_fresh_installation(db: AsyncSession) -> None:
    """Crea datos funcionales que ``create_all`` no puede insertar."""
    if await db.get(ConfiguracionBotModel, 1) is None:
        db.add(ConfiguracionBotModel(
            id=1,
            activo=True,
            palabra_activacion="fdezbot",
            minutos_sesion=15,
            inicio_fuera_horario=True,
            zona_horaria="America/Mexico_City",
            horario_atencion_json=json.dumps(DEFAULT_SCHEDULE, ensure_ascii=False),
            mensaje_fuera_horario=DEFAULT_AWAY_MESSAGE,
            mensaje_bienvenida="Soy tu asistente de pagos y servicios. Elige una opción:",
            mensaje_despedida="Asistente desactivado. Un asesor humano te atenderá a la brevedad.",
            opciones_json=json.dumps(DEFAULT_OPTIONS, ensure_ascii=False),
        ))

    existing_scopes = set((await db.scalars(select(FlujoBotModel.alcance))).all())
    flow_defaults = (
        ("cliente", "Autoservicio de clientes", "fdezbot", PUBLIC_NODES, PUBLIC_EDGES),
        ("tecnico", "Herramientas para técnicos", "tecnico", TECH_NODES, TECH_EDGES),
    )
    for scope, name, command, nodes, edges in flow_defaults:
        if scope not in existing_scopes:
            db.add(FlujoBotModel(
                alcance=scope,
                nombre=name,
                activo=True,
                comando=command,
                nodos_json=json.dumps(nodes, ensure_ascii=False),
                conexiones_json=json.dumps(edges, ensure_ascii=False),
            ))

    template = await db.scalar(
        select(PlantillaMensajeModel).where(PlantillaMensajeModel.tipo == "datos_pago")
    )
    if template is None:
        db.add(PlantillaMensajeModel(
            tipo="datos_pago",
            texto="Configura aquí banco, beneficiario, cuenta o CLABE y referencia de pago.",
            activo=False,
        ))

    plan_defaults = (
        ("demo", "Demo 15 días", "demo", 15, 0, 50, 1),
        ("basico", "Básico", "mensual", 30, 3, 300, 2),
        ("profesional", "Profesional", "mensual", 30, 5, 1000, 10),
        ("ilimitado", "Ilimitado", "permanente", None, 0, None, None),
    )
    existing_plans = set((await db.scalars(select(PlanLicenciaModel.codigo))).all())
    for code, name, kind, duration, grace, client_limit, router_limit in plan_defaults:
        if code not in existing_plans:
            db.add(PlanLicenciaModel(
                codigo=code,
                nombre=name,
                tipo=kind,
                precio_mensual=0,
                duracion_dias=duration,
                dias_gracia=grace,
                limite_clientes=client_limit,
                limite_routers=router_limit,
                activo=True,
            ))
    await db.commit()
