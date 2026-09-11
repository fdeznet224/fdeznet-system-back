import json

from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.models import ConfiguracionBotModel


ALLOWED_ACTIONS = {
    "reportar_pago",
    "promesa_pago",
    "estado_servicio",
    "datos_pago",
    "diagnostico_tecnico",
}
DEFAULT_OPTIONS = [
    {"id": "reportar_pago", "label": "Reportar pago", "enabled": True},
    {"id": "promesa_pago", "label": "Promesa de pago", "enabled": True},
    {"id": "estado_servicio", "label": "Consultar mi servicio y saldo", "enabled": True},
    {"id": "datos_pago", "label": "Datos para depósito o transferencia", "enabled": True},
    {"id": "diagnostico_tecnico", "label": "No tengo internet / diagnóstico técnico", "enabled": True},
]
OPTION_ICONS = {
    "reportar_pago": "💳",
    "promesa_pago": "⏳",
    "estado_servicio": "📊",
    "datos_pago": "🏦",
    "diagnostico_tecnico": "🛠️",
}
DAYS = (
    "lunes",
    "martes",
    "miercoles",
    "jueves",
    "viernes",
    "sabado",
    "domingo",
)
DAY_LABELS = {
    "lunes": "Lunes",
    "martes": "Martes",
    "miercoles": "Miércoles",
    "jueves": "Jueves",
    "viernes": "Viernes",
    "sabado": "Sábado",
    "domingo": "Domingo",
}
DEFAULT_SCHEDULE = {
    day: {
        "activo": day not in {"domingo"},
        "inicio": "09:00" if day == "sabado" else "08:00",
        "fin": "14:00" if day == "sabado" else "20:00",
    }
    for day in DAYS
}
DEFAULT_AWAY_MESSAGE = (
    "🌙 *Estamos fuera del horario de atención.*\n\n"
    "*Horario de atención:*\n{horario}\n\n"
    "🤖 Soy *{asistente}*, el asistente automático de {empresa}. "
    "Mientras regresamos puedo ayudarte con pagos, saldo, promesas y conexión."
)


def normalize_options(raw) -> list[dict]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = []
    if not isinstance(raw, list):
        raw = []
    normalized = []
    seen = set()
    defaults = {item["id"]: item for item in DEFAULT_OPTIONS}
    for item in raw:
        if not isinstance(item, dict):
            continue
        action = str(item.get("id") or "").strip()
        if action not in ALLOWED_ACTIONS or action in seen:
            continue
        label = str(item.get("label") or defaults[action]["label"]).strip()[:80]
        normalized.append({
            "id": action,
            "label": label or defaults[action]["label"],
            "enabled": bool(item.get("enabled", True)),
        })
        seen.add(action)
    for item in DEFAULT_OPTIONS:
        if item["id"] not in seen:
            normalized.append(dict(item))
    return normalized


def normalize_schedule(raw) -> dict[str, dict]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    normalized = {}
    for day in DAYS:
        default = DEFAULT_SCHEDULE[day]
        value = raw.get(day) if isinstance(raw.get(day), dict) else {}
        inicio = str(value.get("inicio") or default["inicio"])
        fin = str(value.get("fin") or default["fin"])
        if not _valid_time(inicio) or not _valid_time(fin) or inicio == fin:
            inicio, fin = default["inicio"], default["fin"]
        normalized[day] = {
            "activo": bool(value.get("activo", default["activo"])),
            "inicio": inicio,
            "fin": fin,
        }
    return normalized


def _valid_time(value: str) -> bool:
    try:
        hours, minutes = (int(part) for part in value.split(":"))
    except (TypeError, ValueError):
        return False
    return 0 <= hours <= 23 and 0 <= minutes <= 59 and len(value) == 5


def out_of_hours_payload(config: ConfiguracionBotModel) -> dict:
    return {
        "habilitado": bool(config.inicio_fuera_horario),
        "zona_horaria": config.zona_horaria or "America/Mexico_City",
        "horario": normalize_schedule(config.horario_atencion_json),
        "mensaje": config.mensaje_fuera_horario or DEFAULT_AWAY_MESSAGE,
    }


def schedule_summary(raw) -> str:
    schedule = normalize_schedule(raw)
    lines = []
    for day in DAYS:
        rule = schedule[day]
        label = DAY_LABELS[day]
        detail = (
            f"{rule['inicio']} a {rule['fin']}"
            if rule["activo"]
            else "cerrado"
        )
        lines.append(f"• {label}: {detail}")
    return "\n".join(lines)


async def get_or_create_bot_config(db: AsyncSession) -> ConfiguracionBotModel:
    config = await db.get(ConfiguracionBotModel, 1)
    if config:
        return config
    config = ConfiguracionBotModel(
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
    )
    db.add(config)
    await db.commit()
    await db.refresh(config)
    return config


def config_payload(config: ConfiguracionBotModel) -> dict:
    return {
        "activo": bool(config.activo),
        "palabra_activacion": config.palabra_activacion or "fdezbot",
        "minutos_sesion": config.minutos_sesion or 15,
        "inicio_fuera_horario": bool(config.inicio_fuera_horario),
        "zona_horaria": config.zona_horaria or "America/Mexico_City",
        "horario_atencion": normalize_schedule(config.horario_atencion_json),
        "mensaje_fuera_horario": config.mensaje_fuera_horario or DEFAULT_AWAY_MESSAGE,
        "fuera_horario": out_of_hours_payload(config),
        "mensaje_bienvenida": config.mensaje_bienvenida,
        "mensaje_despedida": config.mensaje_despedida,
        "opciones": normalize_options(config.opciones_json),
    }


def enabled_options(config: ConfiguracionBotModel | None = None) -> list[dict]:
    options = normalize_options(config.opciones_json if config else DEFAULT_OPTIONS)
    return [item for item in options if item["enabled"]]


def build_bot_menu(
    assistant_name: str = "Asistente",
    config: ConfiguracionBotModel | None = None,
) -> str:
    welcome = (
        config.mensaje_bienvenida
        if config and config.mensaje_bienvenida
        else "Soy tu asistente de pagos y servicios. Elige una opción:"
    )
    keyword = config.palabra_activacion if config else "fdezbot"
    lines = [f"🤖 *Bienvenido a {assistant_name}*", welcome.strip(), ""]
    for number, option in enumerate(enabled_options(config), start=1):
        lines.append(
            f"{number}️⃣ {OPTION_ICONS[option['id']]} *{option['label']}*"
        )
    lines.extend([
        "",
        "👉 Responde con el número de la opción.",
        f"Escribe *{keyword}* o *menú* para volver aquí; *cancelar* para salir.",
    ])
    return "\n".join(lines)


def action_for_input(
    config: ConfiguracionBotModel | None,
    value: str,
) -> str | None:
    if not value.isdigit():
        return None
    index = int(value) - 1
    options = enabled_options(config)
    if index < 0 or index >= len(options):
        return None
    return options[index]["id"]


def action_enabled(config: ConfiguracionBotModel | None, action: str) -> bool:
    return any(item["id"] == action for item in enabled_options(config))
