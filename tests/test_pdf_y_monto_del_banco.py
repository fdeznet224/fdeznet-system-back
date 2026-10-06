"""Comprobantes en PDF, CEP de Banxico y espera del agente."""

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from src.application.services import comprobante_service as comprobante_mod
from src.application.services import ocr_service
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



# ------------------------------------------ CEP a una cuenta que no es del ISP
from src.application.services.comprobante_service import ComprobanteService  # noqa: E402
from src.application.services.ocr_service import terminaciones_de_cuenta  # noqa: E402


def test_el_cep_dice_la_cuenta_beneficiaria():
    assert OCRService.extraer_datos(CEP)["cuenta_beneficiaria"] == "014100605514952222"


def test_terminaciones_de_tarjeta_y_de_clabe():
    assert terminaciones_de_cuenta("4027665833605265") == {"5265"}
    # En la CLABE el último dígito es de control: la cuenta termina en 6342.
    assert terminaciones_de_cuenta("127180000000063429") == {"3429", "6342"}


class _DBComprobantes:
    def __init__(self):
        self.agregados = []

    def add(self, objeto):
        self.agregados.append(objeto)

    async def commit(self):
        return None

    async def refresh(self, objeto):
        objeto.id = 21

    async def execute(self, _consulta):
        class _R:
            def scalars(self):
                return self

            def first(self):
                return None

        return _R()


# Cuentas del ISP que reciben pagos (terminaciones de tarjeta y CLABE).
CONFIG = SimpleNamespace(cuentas_destino_permitidas="6342,5265")


def _leer_cep(beneficiario, monkeypatch):
    class _OCR:
        async def procesar_ticket(self, _url):
            return {**OCRService.extraer_datos(CEP), "cuenta_beneficiaria": beneficiario}

    monkeypatch.setattr(comprobante_mod, "config_pagos", lambda _db: asyncio.sleep(0, CONFIG))
    db = _DBComprobantes()
    resultado = asyncio.run(ComprobanteService(db, ocr=_OCR()).leer(
        "whatsapp-media://document_x.pdf", "1@lid", 191, 9
    ))
    return resultado, db.agregados


def test_un_cep_a_otra_cuenta_no_sirve_como_comprobante(monkeypatch):
    resultado, agregados = _leer_cep("014100605514950199", monkeypatch)  # Santander del propio cliente
    assert resultado["estado"] == "otra_cuenta"
    assert resultado["cuenta_del_comprobante"] == "0199"
    assert agregados[0].estado == "rechazado"
    assert agregados[0].motivo_revision == "beneficiario_no_es_del_isp"


@pytest.mark.parametrize("beneficiario", ["4027665833605265", "127180000000063429"])
def test_un_cep_a_la_tarjeta_o_a_la_clabe_del_isp_si_sirve(beneficiario, monkeypatch):
    resultado, agregados = _leer_cep(beneficiario, monkeypatch)
    assert resultado["estado"] == "leido"
    assert agregados[0].motivo_revision == "esperando_confirmacion"
