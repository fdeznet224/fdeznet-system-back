import asyncio
from datetime import datetime
from types import SimpleNamespace

from src.application.services.chat_orden_service import (
    filtro_mensajes,
    no_leidos_por_orden,
    telefono_para_responder,
    telefonos_de_orden,
)


def _orden(**datos):
    base = dict(id=41, cliente_id=None, prospecto_telefono=None, descripcion=None)
    base.update(datos)
    return SimpleNamespace(**base)


def test_el_prospecto_se_busca_por_su_chat_y_su_telefono():
    orden = _orden(prospecto_telefono="961 123 4567", descripcion="[Agente WhatsApp] Quiere contratar. Chat: 82769002647735@lid")
    assert telefonos_de_orden(orden) == ["82769002647735@lid", "5219611234567", "529611234567", "9611234567"]
    # Se responde al mismo chat donde escribió.
    assert telefono_para_responder(orden) == "82769002647735@lid"


def test_sin_chat_del_agente_se_responde_al_telefono():
    assert telefono_para_responder(_orden(prospecto_telefono="9611234567")) == "5219611234567"
    assert telefono_para_responder(_orden()) is None
    assert filtro_mensajes(_orden()) is None


def test_la_orden_con_cliente_usa_la_conversacion_del_cliente_y_no_la_del_id_de_orden():
    filtro = filtro_mensajes(_orden(id=41, cliente_id=272))
    assert "cliente_id" in str(filtro)
    assert filtro.right.value == 272


def test_no_leidos_se_cuentan_por_orden():
    fecha = datetime(2026, 10, 4, 9, 0)

    class _DB:
        async def execute(self, _consulta):
            return SimpleNamespace(all=lambda: [
                (None, "82769002647735@lid", 3, fecha),
                (272, "5219610000000", 2, fecha),
                (None, "5219999999999", 5, fecha),
            ])

    prospecto = _orden(id=41, descripcion="Chat: 82769002647735@lid")
    cliente = _orden(id=42, cliente_id=272)
    sin_mensajes = _orden(id=43, prospecto_telefono="9618888888")

    resultado = asyncio.run(no_leidos_por_orden(_DB(), [prospecto, cliente, sin_mensajes]))

    assert resultado == {
        "41": {"count": 3, "antiguedad": fecha.isoformat()},
        "42": {"count": 2, "antiguedad": fecha.isoformat()},
    }
