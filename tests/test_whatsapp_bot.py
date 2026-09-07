import asyncio
from datetime import date, datetime
from zoneinfo import ZoneInfo
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.application.services.ocr_service import OCRService
from src.interfaces.api.whatsapp import (
    BOT_KEYWORD,
    construir_menu_bot,
    detectar_intencion_bot,
    esta_fuera_de_horario,
    interpretar_fecha_promesa,
    mensaje_audio_no_disponible,
    mensaje_fuera_de_horario,
    obtener_factura_cobrable,
)


def test_ocr_extrae_folio_monto_y_cedula_de_transferencia():
    resultado = OCRService.extraer_datos(
        "Transferencia exitosa "
        "Clave de rastreo 202607281234567890 "
        "Monto $500.00 MXN "
        "Concepto 329B"
    )

    assert resultado == {
        "folio": "202607281234567890",
        "monto": 500.0,
        "cedula_detectada": "329B",
        "exito": True,
    }


def test_ocr_acepta_monto_sin_centavos():
    resultado = OCRService.extraer_datos(
        "Operación ABC1234567 Total $750 MXN"
    )

    assert resultado["folio"] == "ABC1234567"
    assert resultado["monto"] == 750.0
    assert resultado["exito"] is True


def test_ocr_no_aprueba_texto_sin_folio():
    resultado = OCRService.extraer_datos(
        "Transferencia por $500.00 MXN"
    )

    assert resultado["folio"] is None
    assert resultado["exito"] is False


def test_bot_busca_facturas_pendientes_y_vencidas(monkeypatch):
    factura = SimpleNamespace(id=81)

    class Result:
        def scalars(self):
            return self

        def first(self):
            return factura

    class FakeDB:
        def __init__(self):
            self.statement = None

        async def execute(self, statement):
            self.statement = statement
            return Result()

        async def commit(self):
            return None

    preparar = AsyncMock(return_value=(factura, factura, None))
    monkeypatch.setattr(
        "src.interfaces.api.whatsapp.BillingService.preparar_factura_cobrable",
        preparar,
    )

    db = FakeDB()
    encontrada = asyncio.run(obtener_factura_cobrable(db, 12))
    parametros = db.statement.compile().params

    assert encontrada is factura
    preparar.assert_awaited_once()
    assert ["pendiente", "vencida"] in parametros.values()
    assert 12 in parametros.values()


def test_bot_usa_fdezbot_como_palabra_de_acceso():
    assert BOT_KEYWORD == "fdezbot"
    assert "FdezBot" in construir_menu_bot()
    assert "fdezpay" not in construir_menu_bot().lower()
    assert "No tengo internet" in construir_menu_bot()


def test_bot_acepta_dia_o_fecha_completa_para_promesa():
    hoy = date(2026, 9, 6)

    assert interpretar_fecha_promesa("15", hoy) == date(2026, 9, 15)
    assert interpretar_fecha_promesa("5", hoy) == date(2026, 10, 5)
    assert interpretar_fecha_promesa("20/09/2026", hoy) == date(2026, 9, 20)


def test_bot_respeta_horario_de_atencion():
    tz = ZoneInfo("America/Mexico_City")
    assert esta_fuera_de_horario(datetime(2026, 9, 7, 7, 59, tzinfo=tz))
    assert not esta_fuera_de_horario(datetime(2026, 9, 7, 8, 0, tzinfo=tz))
    assert not esta_fuera_de_horario(datetime(2026, 9, 12, 13, 59, tzinfo=tz))
    assert esta_fuera_de_horario(datetime(2026, 9, 12, 14, 0, tzinfo=tz))
    assert esta_fuera_de_horario(datetime(2026, 9, 13, 10, 0, tzinfo=tz))


def test_bot_informa_horario_y_autoservicio_sin_ia():
    mensaje = mensaje_fuera_de_horario()
    assert "Lunes a viernes" in mensaje
    assert "FdezBot" in mensaje
    assert "disponible ahora" in mensaje


def test_bot_pide_texto_al_recibir_audio():
    mensaje = mensaje_audio_no_disponible()
    assert "no procesa notas de voz" in mensaje
    assert "fdezbot" in mensaje


def test_bot_reconoce_solicitudes_comunes_fuera_de_horario():
    assert detectar_intencion_bot("¿A qué cuenta deposito?") == "datos_pago"
    assert detectar_intencion_bot("No tengo internet") == "sin_internet"
    assert detectar_intencion_bot("pueden reactivar mi servicio") == "promesa"
    assert detectar_intencion_bot("cuánto saldo debo") == "estado"
    assert detectar_intencion_bot("ya pagué") == "pago"
    assert detectar_intencion_bot("buenas noches") == "menu"


def test_foto_de_comprobante_tiene_prioridad_sobre_el_texto():
    assert detectar_intencion_bot(
        "[FOTO_COMPROBANTE]",
        es_comprobante=True,
    ) == "pago"
