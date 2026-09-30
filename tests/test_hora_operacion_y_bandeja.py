"""Hora real de la transferencia, reintentos sin folio y archivo del comprobante."""

import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.application.services.bank_email_service import (
    REINTENTAR_SIN_REFERENCIA,
    BankEmailService,
    _parse_operation_time,
)
from src.application.services.ocr_service import OCRService
from src.interfaces.api import whatsapp


@pytest.mark.parametrize(
    "texto, enviado, esperado",
    [
        # Banco Azteca escribe la hora a 12 horas y sin a.m./p.m.
        ("Fecha y hora 10/Sept/2026, 05:27:29 Comisión", datetime(2026, 9, 10, 11, 27, 29), datetime(2026, 9, 10, 11, 27, 29)),
        ("Fecha y hora 24/Sept/2026, 05:07:32 Comisión", datetime(2026, 9, 24, 23, 7, 33), datetime(2026, 9, 24, 23, 7, 32)),
        # El correo tardó 35 minutos: cuenta la hora de la transferencia.
        ("Fecha y hora 30/Sept/2026, 08:54:59", datetime(2026, 9, 30, 15, 30, 0), datetime(2026, 9, 30, 14, 54, 59)),
        ("Fecha y hora 30/Sep/2026, 8:54 p. m.", datetime(2026, 10, 1, 3, 0, 0), datetime(2026, 10, 1, 2, 54, 0)),
    ],
)
def test_se_usa_la_hora_de_la_operacion_escrita_en_el_correo(texto, enviado, esperado):
    assert _parse_operation_time(texto, enviado) == esperado


def test_sin_hora_en_el_correo_o_muy_distinta_se_usa_la_de_envio():
    assert _parse_operation_time("Recibiste $300.00", datetime(2026, 9, 30, 15, 0)) is None
    assert _parse_operation_time("Fecha y hora 10/Ago/2026, 05:27:29", datetime(2026, 9, 30, 15, 0)) is None


def test_los_comprobantes_sin_folio_se_reintentan_dos_horas():
    import inspect

    fuente = inspect.getsource(BankEmailService.reconcile_pending)
    assert "REINTENTAR_SIN_REFERENCIA" in fuente
    assert REINTENTAR_SIN_REFERENCIA.total_seconds() == 2 * 3600


def test_el_contrato_pegado_a_fdeznet_se_lee_completo():
    datos = OCRService.extraer_datos("Envio exitoso $300.00 Concepto fdeznet2E3A Folio M01576668")
    assert datos["cedula_detectada"] == "2E3A"


class _DB:
    def __init__(self, comprobante):
        self.comprobante = comprobante

    async def get(self, _modelo, _llave):
        return self.comprobante


def test_la_bandeja_muestra_la_imagen_guardada_por_whatsapp():
    uploads = Path(whatsapp.__file__).resolve().parents[3] / "bot_whatsapp" / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    archivo = uploads / "image_prueba_bandeja.jpg"
    archivo.write_bytes(b"jpg")
    try:
        comprobante = SimpleNamespace(media_url="whatsapp-media://image_prueba_bandeja.jpg")
        respuesta = asyncio.run(whatsapp.ver_archivo_comprobante(14, db=_DB(comprobante), current_user=None))
        assert Path(respuesta.path) == archivo.resolve()
    finally:
        archivo.unlink()


def test_no_se_sale_de_la_carpeta_de_whatsapp():
    comprobante = SimpleNamespace(media_url="whatsapp-media://../../.env")
    with pytest.raises(HTTPException):
        asyncio.run(whatsapp.ver_archivo_comprobante(14, db=_DB(comprobante), current_user=None))
