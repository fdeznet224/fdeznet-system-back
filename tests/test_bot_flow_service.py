import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from src.application.services.bot_flow_service import (
    action_for_input,
    build_bot_menu,
    normalize_options,
)
from src.domain.schemas import BotFlowUpdate
from src.interfaces.api.whatsapp import (
    formatear_diagnostico_autoservicio,
    telefono_corresponde_cliente,
)


def bot_config(**overrides):
    values = {
        "mensaje_bienvenida": "Selecciona una opción:",
        "palabra_activacion": "ayuda",
        "opciones_json": json.dumps([
            {"id": "datos_pago", "label": "Cómo pagar", "enabled": True},
            {"id": "estado_servicio", "label": "Mi saldo", "enabled": True},
            {"id": "reportar_pago", "label": "Reportar pago", "enabled": False},
        ]),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_menu_respeta_orden_etiquetas_y_comando():
    menu = build_bot_menu("Mi ISP", bot_config())
    assert "1️⃣ 🏦 *Cómo pagar*" in menu
    assert "2️⃣ 📊 *Mi saldo*" in menu
    assert "Reportar pago" not in menu
    assert "*ayuda*" in menu
    assert action_for_input(bot_config(), "1") == "datos_pago"
    assert action_for_input(bot_config(), "2") == "estado_servicio"
    assert action_for_input(bot_config(), "3") == "promesa_pago"
    assert action_for_input(bot_config(), "6") is None


def test_normalizar_opciones_repara_json_invalido():
    options = normalize_options("no-es-json")
    assert len(options) == 5
    assert options[0]["id"] == "reportar_pago"


def test_schema_rechaza_opciones_duplicadas_o_todas_inactivas():
    base = {
        "activo": True,
        "palabra_activacion": "ayuda",
        "minutos_sesion": 15,
        "inicio_fuera_horario": True,
        "mensaje_bienvenida": "Selecciona una opción",
        "mensaje_despedida": "Gracias por comunicarte",
    }
    with pytest.raises(ValidationError):
        BotFlowUpdate(**base, opciones=[
            {"id": "datos_pago", "label": "Datos de pago", "enabled": True},
            {"id": "datos_pago", "label": "Cuenta bancaria", "enabled": True},
        ])
    with pytest.raises(ValidationError):
        BotFlowUpdate(**base, opciones=[
            {"id": "datos_pago", "label": "Datos de pago", "enabled": False},
        ])


def test_telefono_debe_corresponder_al_cliente():
    cliente = SimpleNamespace(telefono="55 1234-5678")
    assert telefono_corresponde_cliente("5215512345678@c.us", cliente)
    assert not telefono_corresponde_cliente("5215587654321@c.us", cliente)
    assert not telefono_corresponde_cliente("", cliente)


def test_diagnostico_muestra_pppoe_onu_y_potencia_sin_credenciales():
    cliente = SimpleNamespace(nombre="Cliente Demo", estado="activo")
    texto = formatear_diagnostico_autoservicio(cliente, {
        "mikrotik": {
            "disponible": True,
            "pppoe_online": True,
            "uptime": "1d2h",
            "ping_estado": "ok",
            "perdida_porcentaje": 0,
        },
        "olt": {
            "disponible": True,
            "onu_online": True,
            "potencia_rx_dbm": -21.4,
            "potencia_tx_dbm": 2.1,
        },
    })
    assert "Sesión PPPoE:* 🟢 Conectado" in texto
    assert "ONU:* 🟢 En línea" in texto
    assert "-21.4 dBm" in texto
    assert "contraseña" not in texto.lower()
