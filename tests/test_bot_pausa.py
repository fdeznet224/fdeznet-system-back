import asyncio
from datetime import datetime, timedelta

import pytest

from src.application.services.bot_pausa_service import (
    PAUSA_ASESOR,
    bot_en_pausa,
    clave_telefono,
    pausar_bot,
)
from src.infrastructure.models import BotPausaModel
from src.interfaces.api import whatsapp as whatsapp_api


class _DB:
    def __init__(self):
        self.filas = {}

    async def get(self, _model, clave):
        return self.filas.get(clave)

    def add(self, fila):
        self.filas[fila.telefono] = fila


@pytest.mark.parametrize(
    "valor",
    ["5219611234567@c.us", "529611234567", "9611234567", "+52 961 123 4567"],
)
def test_clave_telefono_iguala_formatos(valor):
    assert clave_telefono(valor) == "9611234567"


@pytest.mark.parametrize("valor", ["12345", "", None])
def test_clave_telefono_descarta_identificadores_no_telefonicos(valor):
    assert clave_telefono(valor) is None


def test_un_lid_usa_su_identificador_completo_y_no_choca_con_un_telefono():
    # Un LID no es teléfono: sus últimos 10 dígitos podrían coincidir con
    # el número de otro cliente, por eso se usa completo.
    assert clave_telefono("123456789012345@lid") == "123456789012345"
    assert clave_telefono("123456789012345@lid") != clave_telefono("5216789012345")


def test_pausa_dura_dos_horas_y_se_reconoce_en_cualquier_formato():
    db = _DB()

    hasta = asyncio.run(pausar_bot(db, "5219611234567@c.us", "respuesta_celular"))

    assert PAUSA_ASESOR == timedelta(hours=2)
    assert timedelta(minutes=119) < hasta - datetime.now() <= PAUSA_ASESOR
    assert asyncio.run(bot_en_pausa(db, "9611234567")) is True
    assert asyncio.run(bot_en_pausa(db, "5219610000000@c.us")) is False


def test_pausa_vencida_ya_no_calla_al_bot():
    db = _DB()
    db.add(BotPausaModel(
        telefono="9611234567",
        pausado_hasta=datetime.now() - timedelta(minutes=1),
        motivo="respuesta_panel",
    ))

    assert asyncio.run(bot_en_pausa(db, "9611234567")) is False


def test_nueva_respuesta_extiende_la_pausa_sin_acortarla():
    db = _DB()
    lejana = datetime.now() + timedelta(hours=5)
    db.add(BotPausaModel(telefono="9611234567", pausado_hasta=lejana, motivo="x"))

    asyncio.run(pausar_bot(db, "9611234567", "respuesta_panel"))

    assert db.filas["9611234567"].pausado_hasta == lejana
    assert db.filas["9611234567"].motivo == "respuesta_panel"


def test_responder_cierra_el_menu_activo_del_bot():
    whatsapp_api.bot_memory.clear()
    whatsapp_api.bot_memory["5219611234567@c.us"] = {"paso": "FLUJO_VISUAL"}
    whatsapp_api.bot_memory["5219610000000@c.us"] = {"paso": "FLUJO_VISUAL"}

    whatsapp_api.cerrar_sesion_bot("9611234567")

    assert list(whatsapp_api.bot_memory) == ["5219610000000@c.us"]
    whatsapp_api.bot_memory.clear()


def test_hablar_con_asesor_es_accion_publica():
    from src.application.services.bot_visual_flow_service import PUBLIC_ACTIONS

    assert "hablar_asesor" in PUBLIC_ACTIONS
