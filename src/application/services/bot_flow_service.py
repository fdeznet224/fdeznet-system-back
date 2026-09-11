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
        "mensaje_bienvenida": config.mensaje_bienvenida,
        "mensaje_despedida": config.mensaje_despedida,
        "opciones": normalize_options(config.opciones_json),
    }


def enabled_options(config: ConfiguracionBotModel | None = None) -> list[dict]:
    options = normalize_options(config.opciones_json if config else DEFAULT_OPTIONS)
    return [item for item in options if item["enabled"]]


def build_bot_menu(
    assistant_name: str = "FdezBot",
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
