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


# ------------------------------------------- el agente retoma el chat
from types import SimpleNamespace  # noqa: E402

from src.application.services.bot_pausa_service import (  # noqa: E402
    PAUSA_INTERVENCION,
    anotar_mensaje_en_pausa,
    retomar_chats_pausados,
)

AHORA = datetime(2026, 10, 3, 19, 58, 30)
LID = "136705252319389@lid"


def test_la_pausa_por_intervencion_es_corta():
    assert PAUSA_INTERVENCION == timedelta(minutes=15)


def test_un_mensaje_durante_la_pausa_queda_anotado_y_el_asesor_lo_borra_al_contestar():
    db = _DB()
    # Mónica: el asesor le escribe al número; ella contesta desde su @lid,
    # que el puente resuelve al mismo número.
    asyncio.run(pausar_bot(db, "5213223548024", "respuesta_celular", duracion=PAUSA_INTERVENCION))
    assert asyncio.run(anotar_mensaje_en_pausa(db, "5213223548024@c.us", 13009)) is True
    assert db.filas["3223548024"].mensaje_pendiente_id == 13009
    asyncio.run(pausar_bot(db, "5213223548024", "respuesta_celular", duracion=PAUSA_INTERVENCION))
    assert db.filas["3223548024"].mensaje_pendiente_id is None


def test_sin_pausa_no_se_anota_nada():
    assert asyncio.run(anotar_mensaje_en_pausa(_DB(), "5213223548024@c.us", 1)) is False


class _Lista:
    def __init__(self, valores):
        self.valores = valores

    def scalars(self):
        return self

    def all(self):
        return self.valores


class _DBRetomar:
    def __init__(self, pausas, mensajes):
        self.pausas = pausas
        self.mensajes = {m.id: m for m in mensajes}

    async def execute(self, _consulta):
        return _Lista(self.pausas)

    async def get(self, _modelo, id_):
        return self.mensajes.get(id_)

    async def commit(self):
        return None


def _retomar(mensaje, motivo="respuesta_celular"):
    pausa = SimpleNamespace(telefono="3223548024", motivo=motivo, mensaje_pendiente_id=mensaje.id,
                            pausado_hasta=AHORA - timedelta(minutes=1))
    db = _DBRetomar([pausa], [mensaje])
    lanzados = []
    n = asyncio.run(retomar_chats_pausados(db, lambda *args: lanzados.append(args), AHORA))
    return n, lanzados, pausa


def test_al_vencer_la_pausa_el_agente_atiende_la_foto_pendiente():
    foto = SimpleNamespace(id=13009, direccion="entrada", telefono=LID,
                           mensaje="📷 [Imagen enviada] whatsapp-media://image_998e.jpg")
    n, lanzados, pausa = _retomar(foto)
    assert n == 1
    assert lanzados == [(LID, LID, 13009, "", "whatsapp-media://image_998e.jpg")]
    assert pausa.mensaje_pendiente_id is None  # una sola vez


def test_un_texto_pendiente_se_retoma_con_su_texto():
    texto = SimpleNamespace(id=4, direccion="entrada", telefono=LID, mensaje="Ya le transferí")
    _, lanzados, _ = _retomar(texto)
    assert lanzados == [(LID, LID, 4, "Ya le transferí", None)]


def test_los_audios_los_sigue_escuchando_un_asesor():
    audio = SimpleNamespace(id=3, direccion="entrada", telefono=LID, mensaje="[AUDIO]")
    n, lanzados, _ = _retomar(audio)
    assert n == 0 and lanzados == []
