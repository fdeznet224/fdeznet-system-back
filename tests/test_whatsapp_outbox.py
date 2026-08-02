import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.routing import APIRoute

from src.application.services.whatsapp_outbox_service import (
    WhatsAppOutboxService,
)
from src.infrastructure.whatsapp_client import (
    WhatsAppQueue,
    WhatsAppService,
    estado_por_ack,
)
from src.main import app


def _roles(path: str, method: str) -> set[str]:
    route = next(
        item
        for item in app.routes
        if isinstance(item, APIRoute)
        and item.path == path
        and method in item.methods
    )
    roles = set()

    def recorrer(dependant):
        for dependency in dependant.dependencies:
            cierre = getattr(dependency.call, "__closure__", None)
            if cierre:
                for celda in cierre:
                    if isinstance(celda.cell_contents, set):
                        roles.update(celda.cell_contents)
            recorrer(dependency)

    recorrer(route.dependant)
    return roles


def test_ack_se_traduce_a_estados_operativos():
    assert estado_por_ack(-1) == "fallido"
    assert estado_por_ack(0) == "pendiente"
    assert estado_por_ack(1) == "enviado"
    assert estado_por_ack(2) == "entregado"
    assert estado_por_ack(3) == "leido"
    assert estado_por_ack(4) == "leido"


def test_numero_invalido_devuelve_error_no_reintentable():
    resultado = asyncio.run(
        WhatsAppService().enviar_mensaje_detallado("", "Hola")
    )

    assert resultado["ok"] is False
    assert resultado["reintentable"] is False
    assert resultado["incierto"] is False
    assert "número válido" in resultado["error"]


def test_ack_fuera_de_orden_no_degrada_un_mensaje_leido():
    mensaje = SimpleNamespace(
        id=8,
        direccion="salida",
        ack=3,
        estado_envio="leido",
        wa_id="wa-8",
        bloqueado_hasta=None,
        enviado_en=None,
        entregado_en=None,
        leido_en=None,
        ultimo_error=None,
        proximo_intento_en=None,
        intentos=1,
        max_intentos=5,
    )

    class DB:
        def __init__(self):
            self.commits = 0

        async def get(self, _model, _identificador):
            return mensaje

        async def commit(self):
            self.commits += 1

    db = DB()
    resultado = asyncio.run(
        WhatsAppOutboxService(db).actualizar_ack(
            ack=1,
            wa_id="wa-8",
            mensaje_chat_id=8,
        )
    )

    assert resultado.estado_envio == "leido"
    assert resultado.ack == 3
    assert db.commits == 0

    resultado = asyncio.run(
        WhatsAppOutboxService(db).actualizar_ack(
            ack=-1,
            wa_id="wa-8",
            mensaje_chat_id=8,
        )
    )
    assert resultado.estado_envio == "leido"
    assert resultado.ack == 3
    assert db.commits == 0


def test_reintento_manual_reinicia_el_envio_y_conserva_auditoria():
    mensaje = SimpleNamespace(
        estado_envio="fallido",
        ack=-1,
        wa_id="anterior",
        intentos=5,
        ultimo_error="Bot desconectado",
        proximo_intento_en=None,
        bloqueado_hasta=None,
        reintentos_manuales=2,
        ultimo_reintento_por_id=None,
    )

    WhatsAppOutboxService._preparar_reintento(mensaje, usuario_id=17)

    assert mensaje.estado_envio == "pendiente"
    assert mensaje.ack == 0
    assert mensaje.wa_id is None
    assert mensaje.intentos == 0
    assert mensaje.ultimo_error is None
    assert mensaje.reintentos_manuales == 3
    assert mensaje.ultimo_reintento_por_id == 17


def test_bandeja_y_reintentos_tienen_roles_y_migracion():
    assert _roles("/whatsapp/salidas", "GET") == {
        "admin",
        "supervisor",
    }
    assert _roles(
        "/whatsapp/salidas/{mensaje_id}/reintentar",
        "POST",
    ) == {"admin", "supervisor"}
    assert _roles(
        "/whatsapp/salidas/reintentar-fallidos",
        "POST",
    ) == {"admin", "supervisor"}

    migracion = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "d4e5f6a7b8c9_outbox_whatsapp.py"
    ).read_text(encoding="utf-8")
    assert "estado_envio" in migracion
    assert "proximo_intento_en" in migracion
    assert "reintentos_manuales" in migracion


def test_worker_puede_procesar_tarea_recuperada_solo_con_id(monkeypatch):
    cola = WhatsAppQueue()
    cola._reclamar_registro = AsyncMock(
        return_value={
            "numero": "5215512345678",
            "mensaje": "Mensaje recuperado",
            "ruta": None,
        }
    )
    cola.service.enviar_mensaje_detallado = AsyncMock(
        return_value={
            "ok": True,
            "wa_id": "wa-recuperado",
            "error": None,
            "reintentable": False,
            "incierto": False,
        }
    )
    cola._actualizar_registro = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "src.infrastructure.whatsapp_client.asyncio.sleep",
        AsyncMock(),
    )

    async def ejecutar():
        await cola.queue.put(
            {"mensaje_chat_id": 44, "intervalo": 1}
        )
        await cola.procesar_cola()

    asyncio.run(ejecutar())

    cola.service.enviar_mensaje_detallado.assert_awaited_once_with(
        telefono="5215512345678",
        mensaje="Mensaje recuperado",
        ruta=None,
        mensaje_chat_id=44,
    )
    cola._actualizar_registro.assert_awaited_once()
