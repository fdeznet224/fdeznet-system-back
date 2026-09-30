"""Comprobantes en PDF, monto del banco con el mismo folio y espera del agente."""

import asyncio
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from src.application.services import ocr_service
from src.application.services.bank_email_service import BankEmailService
from src.application.services.ocr_service import OCRService, leer_pdf
from src.interfaces.api import whatsapp


def _pdf_con_texto(lineas: list[str]) -> bytes:
    """PDF mínimo de una página con texto, como el que manda la app del banco."""
    texto = " ".join(f"({linea}) Tj 0 -20 Td" for linea in lineas)
    stream = f"BT /F1 12 Tf 40 780 Td {texto} ET".encode("latin-1")
    objetos = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    salida = b"%PDF-1.4\n"
    posiciones = []
    for numero, objeto in enumerate(objetos, start=1):
        posiciones.append(len(salida))
        salida += f"{numero} 0 obj\n".encode() + objeto + b"\nendobj\n"
    xref = len(salida)
    salida += f"xref\n0 {len(objetos) + 1}\n0000000000 65535 f \n".encode()
    salida += b"".join(f"{p:010d} 00000 n \n".encode() for p in posiciones)
    salida += f"trailer\n<< /Size {len(objetos) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return salida


COMPROBANTE_PDF = _pdf_con_texto([
    "Transferencia exitosa",
    "Monto $300.00 MXN",
    "Clave de rastreo 2026093040014BMOVP020422198180",
    "Concepto INTERNET AGUSTIN VELASCO",
    "30/09/2026 06:08:22",
])


def test_se_lee_el_texto_de_un_pdf_del_banco():
    texto, imagenes = leer_pdf(COMPROBANTE_PDF)
    assert "Clave de rastreo" in texto and imagenes == []


def test_un_comprobante_en_pdf_por_whatsapp_se_interpreta_sin_ocr(tmp_path, monkeypatch):
    (tmp_path / "document_prueba.pdf").write_bytes(COMPROBANTE_PDF)
    monkeypatch.setattr(ocr_service, "WHATSAPP_UPLOADS", tmp_path)
    servicio = OCRService()

    async def sin_ocr():
        raise AssertionError("un PDF con texto no debe pasar por el OCR")

    monkeypatch.setattr(servicio, "_get_reader", sin_ocr)
    datos = asyncio.run(servicio.procesar_ticket("whatsapp-media://document_prueba.pdf"))
    assert datos["exito"] is True
    assert datos["monto"] == 300.0
    assert datos["folio"] == "2026093040014BMOVP020422198180"
    assert datos["fecha_pago"] == datetime(2026, 9, 30, 6, 8, 22)


def test_la_bandeja_tambien_muestra_el_pdf():
    uploads = Path(whatsapp.__file__).resolve().parents[3] / "bot_whatsapp" / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    archivo = uploads / "document_prueba_bandeja.pdf"
    archivo.write_bytes(COMPROBANTE_PDF)

    class _DB:
        async def get(self, *_a):
            return SimpleNamespace(media_url="whatsapp-media://document_prueba_bandeja.pdf")

    try:
        respuesta = asyncio.run(whatsapp.ver_archivo_comprobante(15, db=_DB(), current_user=None))
        assert Path(respuesta.path) == archivo.resolve()
    finally:
        archivo.unlink()


# --------------------------------------------------- monto del banco
CONFIG = SimpleNamespace(activo=True, cuentas_destino_permitidas="6342,5265", tolerancia_monto=Decimal("0"), ventana_dias=3)


def _revision(**cambios):
    datos = dict(
        folio_detectado="2026093040014BMOVPO20422198180",  # el OCR leyó una O por el cero
        monto_detectado=Decimal("30.00"),  # y $30 por $300
        fecha_recepcion=datetime(2026, 9, 30, 16, 1, 37), fecha_pago_detectada=None,
        transaccion_correo_id=None,
    )
    datos.update(cambios)
    return SimpleNamespace(**datos)


def _deposito(**cambios):
    datos = dict(
        id=82, autenticado=True, estado="disponible", tipo_movimiento="entrante",
        cuenta_destino_terminacion="6342", monto=Decimal("300.00"), pago_id=None,
        referencia="2026093040014BMOVP020422198180", fecha_correo=datetime(2026, 9, 30, 12, 8, 22),
    )
    datos.update(cambios)
    return SimpleNamespace(**datos)


def test_con_el_mismo_folio_vale_el_monto_del_banco():
    assert BankEmailService.transaction_match_reason(CONFIG, _revision(), _deposito()) == "coincidencia_exacta"
    assert BankEmailService.motivo_deposito_elegido(CONFIG, _revision(), _deposito()) == "deposito_elegido"


def test_sin_folio_el_monto_leido_debe_cuadrar():
    sin_folio = _revision(folio_detectado=None)
    assert BankEmailService.transaction_match_reason(
        CONFIG, sin_folio, _deposito(), exigir_referencia=False
    ) == "monto_bancario_no_coincide"


def test_con_folio_distinto_no_vale_aunque_el_monto_cuadre():
    otro = _revision(folio_detectado="OTRO12345678", monto_detectado=Decimal("300.00"))
    assert BankEmailService.transaction_match_reason(CONFIG, otro, _deposito()) == "referencia_bancaria_no_coincide"


class _DB:
    def __init__(self, depositos):
        self.depositos = depositos

    async def execute(self, _consulta):
        depositos = self.depositos

        class _R:
            def scalars(self):
                return self

            def all(self):
                return depositos

        return _R()


def test_la_busqueda_por_folio_no_depende_del_monto_leido(monkeypatch):
    servicio = BankEmailService()
    monkeypatch.setattr(servicio, "get_config", lambda _db: asyncio.sleep(0, CONFIG))
    deposito = _deposito()
    otro = _deposito(id=70, referencia="000001971")
    assert asyncio.run(servicio.find_match(_DB([otro, deposito]), _revision())) == (deposito, "coincidencia_exacta")


def test_el_agente_espera_8_segundos_para_juntar_mensajes():
    assert whatsapp.ESPERA_AGENTE_SEGUNDOS == 8


# ---------------------------------------------- CEP de Banxico en PDF
CEP = """horas
Fecha de consulta 30 de septiembre de 2026
COMPROBANTE   ELECTRÓNICO   DE   PAGO
Concepto del pago
Monto
IVA
Referencia numérica
Clave de rastreo
30 de septiembre de 2026
Transferencia
$ 300.00
$ 0.00
300926
ABC1DEFG2HIJ3KLM4NOP
Fecha de operación en el SPEI®
30 de septiembre de 2026
12:45:08 horas
JUAN PEREZ LOPEZ
638180010153531111
BANCO UNO BANCO DOS
JUAN PEREZ LOPEZ
014100605514952222
Cadena Original (información del pago):
""" + "X" * 120 + "\n"


def test_el_cep_de_banxico_se_lee_con_su_formato():
    datos = OCRService.extraer_datos(CEP)
    assert datos["monto"] == 300.0  # no el "30" de la fecha
    assert datos["folio"] == "ABC1DEFG2HIJ3KLM4NOP"  # no el sello digital
    assert datos["fecha_pago"] == datetime(2026, 9, 30, 12, 45, 8)
    assert datos["cuentas"] == ["1111", "2222"]
    assert datos["exito"] is True


def test_un_codigo_de_mas_de_30_caracteres_no_es_folio():
    datos = OCRService.extraer_datos("Pago $300.00 " + "A1" * 40)
    assert datos["folio"] is None


def test_el_monto_no_se_toma_de_la_fecha():
    datos = OCRService.extraer_datos("Monto IVA Clave 30 de septiembre de 2026 $ 300.00")
    assert datos["monto"] == 300.0
