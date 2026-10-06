"""El pago se confirma con los datos de la captura (ya no hay correo del banco)."""

import asyncio
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

import src.application.services.comprobante_service as comprobante_mod
import src.jobs as jobs
from src.application.services.comprobante_service import ComprobanteService, clave_de_transferencia
from src.application.services.ocr_service import OCRService

AHORA = datetime.now().replace(microsecond=0)
CONFIG = SimpleNamespace(validar_pagos_con="captura", ventana_dias=3, cuentas_destino_permitidas="6342,5265", activo=False)
CLIENTE = SimpleNamespace(id=248, nombre="Margarita Moreno Lopez", cedula="D440", estado="activo")
ADMIN = SimpleNamespace(id=1, rol="admin")


def _factura(**cambios):
    hoy = date.today()
    datos = dict(id=1134, cliente_id=248, saldo_pendiente=Decimal("300.00"),
                 periodo_desde=hoy.replace(day=1), fecha_vencimiento=hoy)
    datos.update(cambios)
    return SimpleNamespace(**datos)


def _revision(**cambios):
    datos = dict(
        id=13, estado="pendiente", cliente_id=None, factura_id=None, pago_id=None,
        telefono="95133743751296@lid", folio_detectado=None, monto_detectado=Decimal("300.00"),
        fecha_pago_detectada=AHORA - timedelta(minutes=5), fecha_recepcion=AHORA,
        huella_captura="SC-0123456789ABCDEF", motivo_revision="sin_referencia", notas_revision=None,
        transaccion_correo_id=None, concepto_detectado="margarita moreno lopez", auditoria_banco=None,
        fecha_revision=None,
    )
    datos.update(cambios)
    return SimpleNamespace(**datos)


class _Primero:
    def __init__(self, valor):
        self.valor = valor

    def scalars(self):
        return self

    def first(self):
        return self.valor


class _DB:
    def __init__(self, revision, clave_usada=False, conteos=None):
        self.revision = revision
        self.clave_usada = clave_usada
        self.agregados = []
        # Respuestas en orden: folio usado, otra revisión, pagos del mes, capturas de hoy.
        self.respuestas = list(conteos or [])

    async def scalar(self, _consulta):
        if self.clave_usada:
            return 99
        return self.respuestas.pop(0) if self.respuestas else None

    async def execute(self, _consulta):
        return _Primero(ADMIN)

    async def get(self, _modelo, _llave):
        return self.revision

    def add(self, objeto):
        self.agregados.append(objeto)

    async def commit(self):
        return None

    async def rollback(self):
        return None


def _aplicar(monkeypatch, revision=None, factura=None, clave_usada=False, idempotente=False, conteos=None, cliente=None):
    cobros, alertas = [], []

    class _Billing:
        def __init__(self, _db):
            pass

        async def registrar_pago_completo(self, **kwargs):
            cobros.append(kwargs)
            return {"pago_id": 700, "idempotente": idempotente}

    monkeypatch.setattr(comprobante_mod, "BillingService", _Billing)
    revision = revision or _revision()
    db = _DB(revision, clave_usada, conteos)

    async def avisar(numero, texto):
        alertas.append(texto)

    servicio = ComprobanteService(db, avisar_admin=avisar)

    async def config_alertas(*_a):
        return SimpleNamespace(telefonos_alerta="9611111111")

    monkeypatch.setattr(db, "get", lambda modelo, llave: (
        config_alertas() if modelo.__name__ == "ConfiguracionSistema" else asyncio.sleep(0, revision)
    ))
    resultado = asyncio.run(servicio.aplicar_por_captura(revision, cliente or CLIENTE, factura or _factura(), CONFIG))
    return resultado, revision, db, cobros, alertas


def test_la_misma_captura_da_el_mismo_codigo_aunque_la_reclame_otro_contrato():
    captura = ("Transferencia exitosa $300.00 Cuenta ****8663 Concepto: margarita moreno lopez "
               "30/09/2026 08.58.12")
    para_ella = OCRService.extraer_datos(captura)
    para_otro = OCRService.extraer_datos(captura.replace("margarita moreno lopez", "contrato 2E3A"))
    assert para_ella["huella"] == para_otro["huella"]


def test_el_codigo_es_el_folio_si_la_captura_lo_trae():
    assert clave_de_transferencia(_revision(folio_detectado="M0157666B")) == "M0157666B"
    assert clave_de_transferencia(_revision(folio_detectado="MO1576668")) == "M01576668"  # O por cero
    assert clave_de_transferencia(_revision()) == "SC-0123456789ABCDEF"


def test_una_captura_valida_se_aplica_sin_esperar_al_banco(monkeypatch):
    resultado, revision, db, cobros, _ = _aplicar(monkeypatch)
    assert resultado["aplicado"] is True and resultado["estado"] == "pago_confirmado_por_captura"
    assert cobros[0]["clave_idempotencia"] == "captura:SC-0123456789ABCDEF"
    assert cobros[0]["monto"] == Decimal("300.00")
    assert revision.estado == "aprobado" and revision.motivo_revision == "aprobado_por_captura"
    assert db.agregados[0].folio_banco == "SC-0123456789ABCDEF"  # ya no se puede volver a usar


def test_si_el_codigo_ya_se_uso_rebota_y_se_avisa(monkeypatch):
    resultado, revision, _, cobros, alertas = _aplicar(monkeypatch, clave_usada=True)
    assert resultado["estado"] == "duplicado" and not cobros
    assert revision.estado == "rechazado" and revision.motivo_revision == "captura_ya_utilizada"
    assert "POSIBLE FRAUDE" in alertas[0]


def test_dos_mensajes_a_la_vez_con_la_misma_captura_no_cobran_dos_veces(monkeypatch):
    resultado, revision, _, _, _ = _aplicar(monkeypatch, idempotente=True)
    assert resultado["estado"] == "duplicado" and revision.estado == "rechazado"


def test_el_monto_de_la_captura_debe_cubrir_la_deuda(monkeypatch):
    resultado, revision, _, cobros, _ = _aplicar(monkeypatch, revision=_revision(monto_detectado=Decimal("30.00")))
    assert resultado["estado"] == "monto_menor_a_la_deuda" and not cobros
    assert revision.estado == "pendiente"


def test_sin_folio_ni_hora_no_hay_codigo_y_se_pide_el_comprobante(monkeypatch):
    resultado, *_ , cobros, _ = _aplicar(monkeypatch, revision=_revision(huella_captura=None, fecha_pago_detectada=None))
    assert resultado["estado"] == "captura_incompleta" and not cobros


@pytest.mark.parametrize("desfase, motivo", [(timedelta(days=-5), "captura_antigua"),
                                             (timedelta(hours=2), "fecha_de_captura_invalida")])
def test_la_fecha_de_la_captura_debe_ser_reciente(monkeypatch, desfase, motivo):
    revision = _revision(fecha_pago_detectada=AHORA + desfase)
    resultado, revision, _, cobros, _ = _aplicar(monkeypatch, revision=revision)
    assert resultado["estado"] == motivo and not cobros and revision.estado == "pendiente"


def test_el_pago_del_mes_siguiente_espera_al_dia_1(monkeypatch):
    siguiente = (date.today().replace(day=1) + timedelta(days=32)).replace(day=1)
    resultado, revision, _, cobros, _ = _aplicar(monkeypatch, factura=_factura(periodo_desde=siguiente))
    assert resultado["estado"] == "pago_adelantado" and not cobros
    assert revision.motivo_revision == "pago_adelantado" and revision.cliente_id == 248


def test_la_tarea_de_adelantados_solo_aplica_capturas(monkeypatch):
    llamadas = []

    class _Comprobantes:
        def __init__(self, _db):
            pass

        async def aplicar_adelantados_por_captura(self):
            llamadas.append("adelantados")

    class _Sesion:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_a):
            return None

    monkeypatch.setattr(jobs, "ComprobanteService", _Comprobantes)
    monkeypatch.setattr(jobs, "SessionLocal", _Sesion)
    asyncio.run(jobs.tarea_aplicar_pagos_adelantados())
    assert llamadas == ["adelantados"]


# ------------------------------------------------------------- límites
def test_mas_del_doble_de_la_deuda_lo_revisa_una_persona(monkeypatch):
    resultado, revision, _, cobros, _ = _aplicar(monkeypatch, revision=_revision(monto_detectado=Decimal("900.00")))
    assert resultado["estado"] == "monto_mayor_al_normal" and not cobros and revision.estado == "pendiente"


def test_dos_meses_juntos_si_se_aplican(monkeypatch):
    resultado, *_ = _aplicar(monkeypatch, revision=_revision(monto_detectado=Decimal("600.00")))
    assert resultado["aplicado"] is True


def test_el_segundo_pago_por_captura_del_mes_lo_revisa_una_persona(monkeypatch):
    resultado, _, _, cobros, _ = _aplicar(monkeypatch, conteos=[None, None, 1, 0])
    assert resultado["estado"] == "segundo_pago_del_mes" and not cobros


def test_muchas_capturas_del_mismo_chat_en_un_dia(monkeypatch):
    resultado, _, _, cobros, _ = _aplicar(monkeypatch, conteos=[None, None, 0, 2])
    assert resultado["estado"] == "muchas_capturas_hoy" and not cobros


def test_lo_que_no_se_valida_solo_pide_un_asesor(monkeypatch):
    resultado, revision, _, cobros, _ = _aplicar(monkeypatch, revision=_revision(monto_detectado=Decimal("900.00")))
    assert resultado["requiere_asesor"] is True and not cobros and revision.estado == "pendiente"
    resultado, *_ = _aplicar(monkeypatch)
    assert resultado["aplicado"] is True and "requiere_asesor" not in resultado


def test_avisa_si_el_concepto_no_traia_el_contrato(monkeypatch):
    resultado, *_ = _aplicar(monkeypatch)
    assert resultado["concepto_traia_contrato"] is False
    resultado, *_ = _aplicar(monkeypatch, revision=_revision(concepto_detectado="pago D440"))
    assert resultado["concepto_traia_contrato"] is True


# ------------------------------------------- lo que no valida, a un asesor
def test_el_agente_pasa_a_un_asesor_el_pago_que_no_valida_solo(monkeypatch):
    from src.application.services.agente_ia_service import _Contexto

    pasados = []

    class _Comprobantes:
        def __init__(self, *_a, **_k):
            pass

        async def conciliar(self, _revision_id, _cliente_id):
            return {"aplicado": False, "estado": "segundo_pago_del_mes",
                    "detalle": "Ya tiene un pago por captura este mes", "requiere_asesor": True}

    class _DBAgente:
        async def get(self, _modelo, _llave):
            return _revision(monto_detectado=Decimal("300.00"))

    import src.application.services.agente_ia_service as agente_mod
    monkeypatch.setattr(agente_mod, "ComprobanteService", _Comprobantes)
    contexto = _Contexto(SimpleNamespace(db=_DBAgente()), SimpleNamespace(mensaje_chat_id=5),
                         "automatico", "1@lid", None, None)
    contexto.cliente = CLIENTE

    async def pasar(motivo):
        pasados.append(motivo)
        return {"pasado_a_asesor": True}

    monkeypatch.setattr(contexto, "_pasar_a_humano", pasar)
    resultado = asyncio.run(contexto._aplicar_comprobante(13))
    assert len(pasados) == 1 and "$300.00" in pasados[0] and "Terminal de Cobro" in pasados[0]
    assert "asesor" in resultado["que_decir"]


def test_el_agente_no_molesta_al_asesor_si_el_pago_se_aplico(monkeypatch):
    from src.application.services.agente_ia_service import _Contexto
    import src.application.services.agente_ia_service as agente_mod

    class _Comprobantes:
        def __init__(self, *_a, **_k):
            pass

        async def conciliar(self, _revision_id, _cliente_id):
            return {"aplicado": True, "estado": "pago_confirmado_por_captura", "concepto_traia_contrato": True}

    monkeypatch.setattr(agente_mod, "ComprobanteService", _Comprobantes)
    contexto = _Contexto(SimpleNamespace(db=None), SimpleNamespace(mensaje_chat_id=5),
                         "automatico", "1@lid", None, None)
    contexto.cliente = CLIENTE

    async def pasar(_motivo):
        raise AssertionError("no debía pasar a un asesor")

    monkeypatch.setattr(contexto, "_pasar_a_humano", pasar)
    assert asyncio.run(contexto._aplicar_comprobante(13))["aplicado"] is True


def test_las_cuentas_destino_solo_aceptan_terminaciones():
    from pydantic import ValidationError
    from src.interfaces.api.configuracion import PagosCapturaRequest

    assert PagosCapturaRequest(cuentas_destino_permitidas=" 6342, 5265,6342 ").cuentas_destino_permitidas == "6342,5265"
    with pytest.raises(ValidationError):
        PagosCapturaRequest(cuentas_destino_permitidas="63421")
