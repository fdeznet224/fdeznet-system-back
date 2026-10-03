"""PDF de la factura que se descarga desde Facturación."""

import io
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from pypdf import PdfReader

from src.application.helpers.pdf_generator import generar_factura_pdf

MARCA = SimpleNamespace(empresa_nombre="FdezNet", color_primario="#1e3a8a", color_secundario="#2563eb",
                        empresa_telefono="9610000000", empresa_email=None, empresa_direccion="Chiapas",
                        pie_recibo="Gracias por su pago.")
CLIENTE = SimpleNamespace(nombre="Cliente Prueba", cedula="BD0F", telefono="9611111111", direccion="Centro")


def _factura(**cambios):
    datos = dict(
        id=1138, estado="pendiente", fecha_emision=date(2026, 10, 1), fecha_vencimiento=date(2026, 10, 5),
        fecha_promesa_pago=None, es_promesa_activa=False, concepto="Servicio de internet",
        detalles="Servicio Internet - Plus", descripcion=None, conceptos=[],
        periodo_desde=date(2026, 10, 1), periodo_hasta=date(2026, 10, 31), dias_con_servicio=31,
        dias_sin_servicio=0, ajuste_suspension=0, cargos_adicionales_total=0,
        total=Decimal("420.00"), saldo_pendiente=Decimal("420.00"),
    )
    datos.update(cambios)
    return SimpleNamespace(**datos)


def _texto(contenido: bytes) -> str:
    return " ".join(pagina.extract_text() for pagina in PdfReader(io.BytesIO(contenido)).pages)


def test_la_factura_pendiente_trae_contrato_saldo_y_como_pagar():
    contenido = generar_factura_pdf(_factura(), CLIENTE, MARCA, datos_pago="*Banco:* Azteca\nTarjeta: 4000")
    assert contenido.startswith(b"%PDF")
    texto = _texto(contenido)
    for esperado in ("FACTURA DE SERVICIO", "#001138", "PENDIENTE DE PAGO", "Contrato: BD0F", "MX$420.00",
                     "SALDO PENDIENTE", "CÓMO PAGAR", "Banco: Azteca", "no es un comprobante fiscal"):
        assert esperado in texto, esperado
    assert "*" not in texto.split("CÓMO PAGAR")[1]


def test_la_factura_pagada_no_muestra_datos_de_pago():
    texto = _texto(generar_factura_pdf(_factura(estado="pagada", saldo_pendiente=Decimal("0")), CLIENTE, MARCA,
                                       datos_pago="Banco: Azteca"))
    assert "PAGADA" in texto and "CÓMO PAGAR" not in texto
    assert "Pagado MX$420.00" in texto.replace("\n", " ")


def test_la_pendiente_con_fecha_pasada_sale_como_vencida():
    assert "VENCIDA" in _texto(generar_factura_pdf(_factura(), CLIENTE, MARCA, vencida=True))


def test_nombres_con_simbolos_no_rompen_el_pdf():
    cliente = SimpleNamespace(nombre="Ana <b>& Cía", cedula=None, telefono=None, direccion=None)
    assert "ANA <B>& CÍA" in _texto(generar_factura_pdf(_factura(), cliente, MARCA))


def test_los_emojis_de_la_plantilla_no_salen_como_cuadros():
    datos = "​💳 DATOS PARA TU PAGO\n🏦 Banco: Azteca\n📝 Tu contrato es *BD0F*"
    texto = _texto(generar_factura_pdf(_factura(), CLIENTE, MARCA, datos_pago=datos))
    assert "■" not in texto
    assert "Banco: Azteca" in texto and "Tu contrato es BD0F" in texto
