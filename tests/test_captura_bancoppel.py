"""Comprobante de la app de BanCoppel: fecha sin hora y "Transf." en el concepto."""

from datetime import datetime

from src.application.services.ocr_service import OCRService

BANCOPPEL = (
    "Operación en proceso de validación con Folio 50119141TRANSBPI42008865 Desde: "
    "CUENTA EFECTIVA DIGITAL **** *** 8914 No. de tarjeta *265 Banco AZTECA Tipo de Cuenta "
    "Tarjeta de Débito Beneficiario ARISEL FERNANDEZ Importe $305.00 Concepto Transf. erika gutierrez "
    "Referencia 0905261 Comisión $0.00 de comisión + $0.00 de IVA Fecha de operación 09/05/2026 "
    "Tu Clave de rastreo es: 50119141TRANSBPI42008865 BanCoppel"
)


def test_se_leen_folio_monto_y_fecha_aunque_no_traiga_hora():
    datos = OCRService.extraer_datos(BANCOPPEL)
    assert datos["exito"] and datos["folio"] == "50119141TRANSBPI42008865"
    assert datos["monto"] == 305.0
    # La fecha dice si la captura es vieja; sin hora no hay huella.
    assert datos["fecha_pago"] == datetime(2026, 5, 9)
    assert datos["huella"] is None


def test_transf_en_el_concepto_no_es_un_contrato():
    assert OCRService.extraer_datos(BANCOPPEL)["cedula_detectada"] is None


def test_el_contrato_despues_de_transf_si_se_toma():
    texto = BANCOPPEL.replace("Transf. erika gutierrez", "Transf. D440")
    assert OCRService.extraer_datos(texto)["cedula_detectada"] == "D440"
