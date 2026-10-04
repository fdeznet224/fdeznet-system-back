"""Comprobantes que se cierran solos y fotos que no son comprobantes."""

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from src.application.services.comprobante_service import ComprobanteService, clave_de_transferencia
from src.application.services.ocr_service import OCRService

CLIENTE = SimpleNamespace(id=221, telefono="9618469982")


class _Lista:
    def __init__(self, valores):
        self.valores = valores

    def scalars(self):
        return self

    def all(self):
        return self.valores

    def first(self):
        return self.valores[0] if self.valores else None


class _DB:
    def __init__(self, revisiones, chats=("240076055404654@lid",)):
        self.respuestas = [list(chats), revisiones]
        self.agregados = []

    async def get(self, _modelo, _id):
        return CLIENTE

    async def execute(self, _consulta):
        return _Lista(self.respuestas.pop(0))

    async def scalar(self, _consulta):
        return None

    def add(self, objeto):
        self.agregados.append(objeto)

    async def commit(self):
        return None

    async def refresh(self, objeto):
        objeto.id = 31


def _revision(**datos):
    base = dict(id=30, estado="pendiente", pago_id=None, cliente_id=None, telefono="240076055404654@lid",
                monto_detectado=Decimal("300.00"), folio_detectado="MBANO10O2610050052630959",
                huella_captura=None, fecha_recepcion=datetime.now() - timedelta(hours=3),
                motivo_revision="esperando_confirmacion", notas_revision=None, fecha_revision=None)
    base.update(datos)
    return SimpleNamespace(**base)


def _resolver(revisiones, monto, pago_ids=(959,)):
    db = _DB(revisiones)
    cerrados = asyncio.run(ComprobanteService(db).resolver_por_pago_manual(221, list(pago_ids), monto, "VG"))
    return cerrados, db


def test_el_pago_manual_cierra_el_comprobante_del_chat_verificado():
    # Breidi: mandó la captura, no dio su contrato y VG registró el pago a mano.
    revision = _revision()
    cerrados, db = _resolver([revision], Decimal("300.00"))
    assert cerrados == [30]
    assert revision.estado == "aprobado" and revision.motivo_revision == "pagado_manualmente"
    assert revision.pago_id == 959 and revision.cliente_id == 221
    assert revision.notas_revision == "Pagado manualmente (pago #959) por VG"
    # La misma captura ya no se puede volver a cobrar.
    assert db.agregados[0].folio_banco == clave_de_transferencia(revision)


def test_dos_comprobantes_que_suman_el_pago_se_cierran_juntos():
    uno = _revision(id=28, monto_detectado=Decimal("252.00"), folio_detectado=None, huella_captura="SC-A")
    otro = _revision(id=29, monto_detectado=Decimal("100.00"), folio_detectado=None, huella_captura="SC-B")
    cerrados, _ = _resolver([uno, otro], Decimal("352.00"))
    assert sorted(cerrados) == [28, 29]
    assert {uno.estado, otro.estado} == {"aprobado"}
    assert [uno.pago_id, otro.pago_id].count(959) == 1


def test_no_cierra_comprobantes_de_otro_cliente_ni_de_otro_monto():
    ajeno = _revision(telefono="999999999999@lid")
    otro_monto = _revision(id=31, monto_detectado=Decimal("150.00"))
    cerrados, _ = _resolver([ajeno, otro_monto], Decimal("300.00"))
    assert cerrados == []
    assert ajeno.estado == "pendiente" and otro_monto.estado == "pendiente"


def test_reconoce_el_numero_registrado_del_cliente():
    revision = _revision(telefono="5219618469982")
    cerrados, _ = _resolver([revision], Decimal("300.00"))
    assert cerrados == [30]


class _OCRFotoModem:
    async def procesar_ticket(self, _url):
        return OCRService.extraer_datos("Router Huawei HG8145V5 LOS PON LAN1 LAN2")


def test_una_foto_que_no_es_comprobante_no_queda_pendiente():
    db = _DB([])
    servicio = ComprobanteService(db, ocr=_OCRFotoModem(), correo=SimpleNamespace())
    resultado = asyncio.run(servicio.leer("whatsapp-media://modem.jpg", "111@lid", None, 5))
    revision = db.agregados[0]
    assert resultado["estado"] == "ilegible"
    assert revision.estado == "rechazado" and revision.motivo_revision == "no_es_comprobante"
