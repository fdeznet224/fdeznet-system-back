import asyncio

from src.application.services.ocr_service import OCRService
from src.interfaces.api.whatsapp import obtener_factura_cobrable


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


def test_bot_busca_facturas_pendientes_y_vencidas():
    factura = object()

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

    db = FakeDB()
    encontrada = asyncio.run(obtener_factura_cobrable(db, 12))
    parametros = db.statement.compile().params

    assert encontrada is factura
    assert ["pendiente", "vencida"] in parametros.values()
    assert 12 in parametros.values()
