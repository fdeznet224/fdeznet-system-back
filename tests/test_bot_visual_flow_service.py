import json
from types import SimpleNamespace

import pytest

from src.application.services.bot_visual_flow_service import (
    execute_until_wait,
    select_menu_target,
    validate_flow_for_scope,
)
from src.application.services.user_service import UserService
from src.interfaces.api.whatsapp import formatear_ficha_red_tecnica


NODES = [
    {"id": "start", "type": "trigger", "title": "Inicio", "text": "", "x": 0, "y": 0},
    {"id": "hello", "type": "message", "title": "Saludo", "text": "Hola técnico", "x": 100, "y": 0},
    {"id": "menu", "type": "menu", "title": "Menú", "text": "Elige", "x": 200, "y": 0},
    {"id": "diag", "type": "action", "title": "Diagnóstico", "text": "", "action": "tecnico_diagnostico", "x": 300, "y": 0},
]
EDGES = [
    {"id": "e1", "source": "start", "target": "hello", "label": ""},
    {"id": "e2", "source": "hello", "target": "menu", "label": ""},
    {"id": "e3", "source": "menu", "target": "diag", "label": "Diagnosticar"},
]


def flow():
    return SimpleNamespace(
        nodos_json=json.dumps(NODES),
        conexiones_json=json.dumps(EDGES),
    )


def test_ejecuta_mensajes_y_espera_seleccion_de_menu():
    validate_flow_for_scope("tecnico", NODES, EDGES)
    result = execute_until_wait(flow())
    assert result["kind"] == "menu"
    assert result["messages"] == ["Hola técnico", "Elige\n\n1. Diagnosticar"]
    assert select_menu_target(flow(), "menu", "1") == "diag"
    assert execute_until_wait(flow(), "diag")["action"] == "tecnico_diagnostico"


def test_rechaza_acciones_de_cliente_en_flujo_tecnico():
    invalid = [dict(node) for node in NODES]
    invalid[-1]["action"] = "estado_servicio"
    with pytest.raises(ValueError, match="Acción no permitida"):
        validate_flow_for_scope("tecnico", invalid, EDGES)


def test_rechaza_bloques_desconectados():
    extra = NODES + [{
        "id": "orphan", "type": "end", "title": "Huérfano",
        "text": "", "x": 400, "y": 0,
    }]
    with pytest.raises(ValueError, match="conectados"):
        validate_flow_for_scope("tecnico", extra, EDGES)


def test_normaliza_telefono_de_staff():
    assert UserService.normalizar_telefono_whatsapp("+52 55 1234-5678") == "525512345678"
    with pytest.raises(ValueError):
        UserService.normalizar_telefono_whatsapp("123")


def test_ficha_tecnica_no_expone_password():
    router = SimpleNamespace(nombre="Nodo Norte")
    olt = SimpleNamespace(nombre="OLT 1")
    onu = SimpleNamespace(identificador="ONU-ABC")
    nap = SimpleNamespace(nombre="NAP-08")
    cliente = SimpleNamespace(
        nombre="Cliente Uno", cedula="C-10", direccion="Calle 1",
        estado="activo", router=router, olt=olt, onu_asignada=onu,
        caja_nap=nap, puerto_nap=4, user_pppoe="cliente10",
        ip_asignada="10.0.0.10", pass_pppoe="secreto",
    )
    text = formatear_ficha_red_tecnica(cliente)
    assert "NAP-08" in text and "Puerto NAP:* 4" in text
    assert "cliente10" in text
    assert "secreto" not in text
