"""Lectura del contrato, archivos del chat y sesión técnica del bot."""

import asyncio
from datetime import datetime

import pytest
from fastapi import HTTPException

from src.application.services.ocr_service import OCRService
from src.interfaces.api import whatsapp


def test_el_contrato_pegado_a_fdeznet_se_lee_completo():
    datos = OCRService.extraer_datos("Envio exitoso $300.00 Concepto fdeznet2E3A Folio M01576668")
    assert datos["cedula_detectada"] == "2E3A"


def test_los_archivos_del_chat_no_se_salen_de_la_carpeta_de_whatsapp(monkeypatch):
    async def acceso_permitido(*_a):
        return None

    monkeypatch.setattr(whatsapp, "verificar_acceso_cliente", acceso_permitido)
    with pytest.raises(HTTPException) as error:
        asyncio.run(whatsapp.ver_archivo_chat(5, "../../.env", db=None, current_user=None))
    assert error.value.status_code == 404


# ------------------------------------------------ bot técnico con el agente
def test_con_sesion_tecnica_abierta_no_contesta_el_agente(monkeypatch):
    monkeypatch.setattr(whatsapp, "bot_memory", {
        "tec@lid": {"paso": "FLUJO_VISUAL", "alcance": "tecnico", "staff_id": 2, "iniciado_en": datetime.now()},
        "cliente@lid": {"paso": "FLUJO_VISUAL", "alcance": "cliente", "staff_id": None, "iniciado_en": datetime.now()},
    })
    assert whatsapp.sesion_tecnica_activa("tec@lid", 10) is True
    assert whatsapp.sesion_tecnica_activa("cliente@lid", 10) is False
    assert whatsapp.sesion_tecnica_activa("otro@lid", 10) is False


def test_la_sesion_tecnica_vencida_regresa_al_agente(monkeypatch):
    from datetime import timedelta

    memoria = {"tec@lid": {"paso": "TECNICO_CONTRATO_PPPOE", "staff_id": 2, "iniciado_en": datetime.now() - timedelta(minutes=11)}}
    monkeypatch.setattr(whatsapp, "bot_memory", memoria)
    assert whatsapp.sesion_tecnica_activa("tec@lid", 10) is False
    assert "tec@lid" not in memoria


def test_el_agente_respeta_la_sesion_tecnica_en_el_webhook():
    import inspect

    fuente = inspect.getsource(whatsapp.webhook_recibir_mensaje)
    assert fuente.index("sesion_tecnica_activa(") < fuente.index("lanzar_agente(")
