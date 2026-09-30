"""Folio propio de las capturas sin referencia para detectar reenvíos."""

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

import src.application.services.agente_ia_service as agente_mod
from src.application.services.agente_ia_service import AgenteIAService, _Contexto
from src.application.services.comprobante_service import ComprobanteService
from src.application.services.ocr_service import OCRService, huella_captura

CAPTURA = (
    "iTu transferencia fue exitosa! Envié $ 300.00 MN Cuenta ****8663 "
    "Tarjeta casa ** **5265 Concepto de transferencia: margarita moreno lopez "
    "30/09/2026 08.58.12 Compartir"
)


def test_la_captura_sin_folio_genera_huella_con_monto_hora_y_cuentas():
    datos = OCRService.extraer_datos(CAPTURA)
    assert datos["cuentas"] == ["5265", "8663"]
    assert datos["huella"] == huella_captura(300.0, datetime(2026, 9, 30, 8, 58, 12), ["8663", "5265"])
    assert datos["huella"].startswith("SC-") and len(datos["huella"]) == 19


def test_la_misma_captura_da_la_misma_huella_aunque_el_ocr_varie_el_texto():
    otra_lectura = CAPTURA.replace("margarita moreno lopez", "margarlta moreno 1opez").replace("iTu", "¡Tu")
    assert OCRService.extraer_datos(otra_lectura)["huella"] == OCRService.extraer_datos(CAPTURA)["huella"]


@pytest.mark.parametrize(
    "cambio",
    [("300.00", "350.00"), ("08.58.12", "08.58.13"), ("30/09/2026", "30/10/2026"), ("5265", "5266")],
)
def test_otra_transferencia_da_otra_huella(cambio):
    assert OCRService.extraer_datos(CAPTURA.replace(*cambio))["huella"] != OCRService.extraer_datos(CAPTURA)["huella"]


def test_sin_hora_no_hay_huella_porque_el_monto_se_repite_cada_mes():
    assert OCRService.extraer_datos("Transferencia por $300.00 MXN Cuenta ****8663")["huella"] is None


class _Resultado:
    def __init__(self, objeto):
        self.objeto = objeto

    def scalars(self):
        return self

    def first(self):
        return self.objeto


class _DB:
    def __init__(self, previa=None):
        self.previa = previa
        self.agregados = []

    async def execute(self, _sentencia):
        return _Resultado(self.previa)

    def add(self, objeto):
        self.agregados.append(objeto)

    async def commit(self):
        return None

    async def refresh(self, objeto):
        objeto.id = 99


class _OCR:
    async def procesar_ticket(self, _url):
        return OCRService.extraer_datos(CAPTURA)


def _leer(previa):
    db = _DB(previa)
    servicio = ComprobanteService(db, ocr=_OCR(), correo=SimpleNamespace())
    resultado = asyncio.run(servicio.leer("whatsapp-media://nueva.jpg", "111@lid", 248, 5))
    return resultado, db


def test_la_primera_vez_se_guarda_la_huella():
    resultado, db = _leer(None)
    assert resultado["estado"] == "sin_referencia"
    assert db.agregados[0].huella_captura == OCRService.extraer_datos(CAPTURA)["huella"]


def test_reenviar_una_captura_ya_pagada_se_marca_duplicada():
    previa = SimpleNamespace(id=13, estado="aprobado", pago_id=501, media_url="whatsapp-media://vieja.jpg", telefono="111@lid")
    resultado, db = _leer(previa)
    assert resultado["estado"] == "duplicado"
    assert resultado["pago_ya_registrado"] is True
    assert resultado["folio"].startswith("SC-")
    assert db.agregados == []


def test_una_captura_rechazada_sin_pago_se_puede_volver_a_revisar():
    previa = SimpleNamespace(id=13, estado="rechazado", pago_id=None, media_url="x", telefono="111@lid")
    resultado, _ = _leer(previa)
    assert resultado["estado"] == "sin_referencia"


def _agente_lee(duplicado):
    avisos = []
    servicio = AgenteIAService(_DB())
    contexto = _Contexto(servicio, SimpleNamespace(mensaje_chat_id=5), "automatico", "111@lid", None, "whatsapp-media://nueva.jpg")

    class _Comprobantes:
        def __init__(self, *_a, **_k):
            pass

        async def leer(self, *_a):
            return {"estado": "duplicado", "folio": "SC-ABC", **duplicado}

        async def alertar_folio_duplicado(self, telefono, folio):
            avisos.append((telefono, folio))

    return contexto, _Comprobantes, avisos


@pytest.mark.parametrize(
    "duplicado, alerta",
    [
        ({"pago_ya_registrado": True, "otro_telefono": False}, True),
        ({"pago_ya_registrado": False, "otro_telefono": True}, True),
        ({"pago_ya_registrado": False, "otro_telefono": False}, False),
        ({"pago_ya_registrado": True, "misma_imagen": True}, False),
    ],
)
def test_solo_se_alerta_fraude_si_ya_se_pago_o_viene_de_otro_numero(monkeypatch, duplicado, alerta):
    contexto, comprobantes, avisos = _agente_lee(duplicado)
    monkeypatch.setattr(agente_mod, "ComprobanteService", comprobantes)
    asyncio.run(contexto._leer_comprobante())
    assert bool(avisos) is alerta
