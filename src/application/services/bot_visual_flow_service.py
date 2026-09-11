import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.models import FlujoBotModel


FLOW_SCOPES = {"cliente", "tecnico"}
PUBLIC_ACTIONS = {
    "reportar_pago",
    "promesa_pago",
    "estado_servicio",
    "datos_pago",
    "diagnostico_tecnico",
}
TECH_ACTIONS = {
    "tecnico_diagnostico",
    "tecnico_pppoe",
    "tecnico_potencia",
    "tecnico_red",
}


def flow_payload(flow: FlujoBotModel, bot_config=None) -> dict:
    payload = {
        "alcance": flow.alcance,
        "nombre": flow.nombre,
        "activo": bool(flow.activo),
        "comando": flow.comando,
        "nodos": json.loads(flow.nodos_json),
        "conexiones": json.loads(flow.conexiones_json),
    }
    if bot_config is not None and flow.alcance == "cliente":
        from src.application.services.bot_flow_service import out_of_hours_payload

        payload["fuera_horario"] = out_of_hours_payload(bot_config)
    return payload


async def get_visual_flow(
    db: AsyncSession,
    scope: str,
) -> FlujoBotModel | None:
    if scope not in FLOW_SCOPES:
        return None
    return (
        await db.execute(
            select(FlujoBotModel).where(FlujoBotModel.alcance == scope)
        )
    ).scalar_one_or_none()


def validate_flow_for_scope(scope: str, nodes: list[dict], edges: list[dict]) -> None:
    if scope not in FLOW_SCOPES:
        raise ValueError("Alcance de flujo inválido")
    allowed = PUBLIC_ACTIONS if scope == "cliente" else TECH_ACTIONS
    by_id = {node["id"]: node for node in nodes}
    outgoing: dict[str, list[dict]] = {}
    for edge in edges:
        outgoing.setdefault(edge["source"], []).append(edge)
    for node in nodes:
        node_type = node["type"]
        node_edges = outgoing.get(node["id"], [])
        if node_type == "action" and node.get("action") not in allowed:
            raise ValueError(f"Acción no permitida en el flujo {scope}")
        if node_type == "menu":
            if not node_edges:
                raise ValueError(f"El menú '{node['title']}' necesita opciones")
            if any(not edge.get("label", "").strip() for edge in node_edges):
                raise ValueError("Cada salida de un menú necesita un texto visible")
        elif node_type not in {"action", "end"} and len(node_edges) != 1:
            raise ValueError(
                f"El bloque '{node['title']}' debe tener exactamente una salida"
            )
        elif node_type in {"action", "end"} and node_edges:
            raise ValueError(f"El bloque final '{node['title']}' no admite salidas")
    trigger = next(node for node in nodes if node["type"] == "trigger")
    visited = set()
    pending = [trigger["id"]]
    while pending:
        node_id = pending.pop()
        if node_id in visited:
            continue
        visited.add(node_id)
        pending.extend(edge["target"] for edge in outgoing.get(node_id, []))
    unreachable = set(by_id) - visited
    if unreachable:
        raise ValueError("Todos los bloques deben estar conectados al inicio")


def execute_until_wait(
    flow: FlujoBotModel,
    start_node_id: str | None = None,
) -> dict:
    nodes = {item["id"]: item for item in json.loads(flow.nodos_json)}
    edges = json.loads(flow.conexiones_json)
    outgoing: dict[str, list[dict]] = {}
    for edge in edges:
        outgoing.setdefault(edge["source"], []).append(edge)
    current = (
        nodes.get(start_node_id)
        if start_node_id
        else next((node for node in nodes.values() if node["type"] == "trigger"), None)
    )
    messages = []
    for _ in range(30):
        if not current:
            return {"kind": "end", "messages": messages}
        node_type = current["type"]
        if node_type == "message" and current.get("text", "").strip():
            messages.append(current["text"].strip())
        if node_type == "menu":
            options = outgoing.get(current["id"], [])
            lines = [current.get("text", "").strip(), ""]
            lines.extend(
                f"{index}. {edge['label']}"
                for index, edge in enumerate(options, start=1)
            )
            messages.append("\n".join(line for line in lines if line or line == ""))
            return {
                "kind": "menu",
                "messages": messages,
                "node_id": current["id"],
                "options": options,
            }
        if node_type == "action":
            return {
                "kind": "action",
                "messages": messages,
                "action": current.get("action"),
            }
        if node_type == "end":
            return {"kind": "end", "messages": messages}
        next_edges = outgoing.get(current["id"], [])
        current = nodes.get(next_edges[0]["target"]) if next_edges else None
    return {
        "kind": "end",
        "messages": messages + ["El flujo alcanzó su límite de seguridad."],
    }


def select_menu_target(flow: FlujoBotModel, node_id: str, value: str) -> str | None:
    if not value.isdigit():
        return None
    edges = [
        edge
        for edge in json.loads(flow.conexiones_json)
        if edge["source"] == node_id
    ]
    index = int(value) - 1
    if index < 0 or index >= len(edges):
        return None
    return edges[index]["target"]
