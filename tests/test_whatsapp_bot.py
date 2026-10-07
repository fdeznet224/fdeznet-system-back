from datetime import datetime
import asyncio
import json
from datetime import date, datetime
from zoneinfo import ZoneInfo
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.application.services.ocr_service import OCRService
from src.interfaces.api.whatsapp import (
    BOT_KEYWORD,
    esta_fuera_de_horario,
    interpretar_fecha_promesa,
    mensaje_comprobante_ya_recibido,
    mensaje_fuera_de_horario,
    obtener_factura_cobrable,
    renderizar_mensaje_campana,
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
        "fecha_pago": None,
        "cuentas": [],
        "concepto": "329b",
        "huella": None,
        "parece_comprobante": True,
        "exito": True,
    }


def test_ocr_lee_la_hora_de_una_captura_sin_referencia():
    # Pantalla "¡Tu transferencia fue exitosa!": trae monto y hora, no folio.
    resultado = OCRService.extraer_datos(
        "iTu transferencia fue exitosa! Envié $ 300.00 MN "
        "Concepto de transferencia: margarita moreno lopez 30/09/2026 08.58.12 Compartir"
    )
    assert resultado["folio"] is None and resultado["monto"] == 300.0
    assert resultado["fecha_pago"] == datetime(2026, 9, 30, 8, 58, 12)
    assert resultado["exito"] is False


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


def test_bot_no_responde_audios():
    import inspect
    from src.interfaces.api import whatsapp

    fuente = inspect.getsource(whatsapp.webhook_recibir_mensaje)
    audio = fuente.index('"[AUDIO]"')
    assert fuente.index("audio_para_asesor") > audio
    # Se revisa antes que cualquier respuesta automática.
    assert audio < fuente.index("mensaje_fuera_de_horario(")
    assert audio < fuente.index("bot_en_pausa(")


def test_bot_no_acepta_comprobante_duplicado_pendiente_o_aprobado():
    pendiente = mensaje_comprobante_ya_recibido("pendiente")
    aprobado = mensaje_comprobante_ya_recibido(
        "pendiente",
        pago_registrado=True,
    )

    assert "continúa en revisión" in pendiente
    assert "No es necesario enviarlo nuevamente" in pendiente
    assert "ya fue aprobado" in aprobado
    assert "No se aplicará el pago otra vez" in aprobado


def test_plantillas_usan_numero_de_contrato_sin_romper_variable_anterior():
    cliente = SimpleNamespace(
        cedula="329B",
        zona=None,
        router=None,
        plan=None,
    )

    mensaje = renderizar_mensaje_campana(
        "Contrato: {contrato}; compatibilidad: {cedula}",
        nombre="Cliente",
        numero="529611111111",
        cliente=cliente,
    )

    assert mensaje == "Contrato: 329B; compatibilidad: 329B"


def test_ocr_lee_las_fechas_de_azteca_con_cero_y_de_mercado_pago():
    azteca = OCRService.extraer_datos("Monto $ 500.00 Fecha 07/0ct/2026 13.37.33 (CST) Folio 123456789012")
    assert azteca["fecha_pago"] == datetime(2026, 10, 7, 13, 37, 33)
    mp = OCRService.extraer_datos("Le transferiste $ 350 Número de operación 128475639201 7 de octubre de 2026, 10:15 hs")
    assert mp["fecha_pago"] == datetime(2026, 10, 7, 10, 15)


def test_una_promocion_con_precios_no_parece_comprobante():
    promo = OCRService.extraer_datos("Planes de internet 50 Megas $350 100 Megas $500 Instalación gratis")
    assert promo["parece_comprobante"] is False
    # "megas 350": la "s" al final de una palabra no es un "$" mal leído.
    assert OCRService.extraer_datos("Plan 50 megas 350")["monto"] == 0.0
    oxxo = OCRService.extraer_datos("OXXO DEPOSITO A TARJETA IMPORTE 400.00 AUTORIZACION 845123 07/10/2026 09:41")
    assert oxxo["parece_comprobante"] is True and oxxo["monto"] == 400.0


def test_mercado_pago_no_toma_el_monto_de_la_fecha_ni_de_la_hora():
    # Producción leía $7 ("Martes 7") y $21 ("las 21:04"): la "s" parecía un "$".
    martes = OCRService.extraer_datos(
        "Comprobante de transferencia Martes 7 de octubre de 2026 a las 10:15 hs $ 350 "
        "Número de operación 128475639201"
    )
    assert (martes["monto"], martes["fecha_pago"]) == (350.0, datetime(2026, 10, 7, 10, 15))
    noche = OCRService.extraer_datos(
        "Comprobante de transferencia Miércoles, 8 de octubre de 2026 a las 21:04 hs S 500 "
        "Número de operación 12847563920"
    )
    assert noche["monto"] == 500.0
    assert OCRService.extraer_datos("Transferencia $ 10:15 hs")["monto"] == 0.0
