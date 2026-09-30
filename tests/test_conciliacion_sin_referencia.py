"""Capturas sin folio: se empatan con el correo por monto y hora cercana."""

import asyncio
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

from src.application.services.bank_email_service import BankEmailService, senales_de_titular

CONFIG = SimpleNamespace(activo=True, cuentas_destino_permitidas="6342, 5265", tolerancia_monto=Decimal("0"), ventana_dias=3)


def _revision(**cambios):
    datos = dict(
        folio_detectado=None, monto_detectado=Decimal("300.00"),
        fecha_pago_detectada=datetime(2026, 9, 30, 8, 58, 12),  # hora local de la captura
        fecha_recepcion=datetime(2026, 9, 30, 9, 2, 31),
        cliente_id=None, concepto_detectado=None, cuentas_detectadas=None,
    )
    datos.update(cambios)
    return SimpleNamespace(**datos)


def _correo(**cambios):
    datos = dict(
        autenticado=True, estado="disponible", tipo_movimiento="entrante",
        cuenta_destino_terminacion="5265", monto=Decimal("300.00"),
        fecha_correo=datetime(2026, 9, 30, 14, 54, 59),  # UTC = 08:54:59 en México
        referencia="AZ123456",
    )
    datos.update(cambios)
    return SimpleNamespace(**datos)


def test_caso_margarita_se_empata_por_monto_y_hora():
    assert BankEmailService.motivo_coincidencia(CONFIG, _revision(), _correo()) == "coincidencia_sin_referencia"


def test_fuera_de_15_minutos_no_se_empata():
    lejos = _correo(fecha_correo=datetime(2026, 9, 30, 15, 30, 0))  # 09:30 en México
    assert BankEmailService.motivo_coincidencia(CONFIG, _revision(), lejos) == "fecha_hora_bancaria_fuera_de_ventana"


def test_sin_referencia_se_siguen_exigiendo_cuenta_monto_y_correo_autentico():
    assert BankEmailService.motivo_coincidencia(CONFIG, _revision(), _correo(cuenta_destino_terminacion="1111")) == "cuenta_destino_no_autorizada"
    assert BankEmailService.motivo_coincidencia(CONFIG, _revision(), _correo(monto=Decimal("350.00"))) == "monto_bancario_no_coincide"
    assert BankEmailService.motivo_coincidencia(CONFIG, _revision(), _correo(autenticado=False)) == "correo_bancario_no_autenticado"
    assert BankEmailService.motivo_coincidencia(CONFIG, _revision(), _correo(estado="conciliada")) == "correo_bancario_ya_utilizado"


def test_con_referencia_se_sigue_exigiendo_que_coincida():
    con_folio = _revision(folio_detectado="OTRO999")
    assert BankEmailService.motivo_coincidencia(CONFIG, con_folio, _correo()) == "referencia_bancaria_no_coincide"


def test_sin_hora_en_la_captura_se_usa_la_hora_en_que_llego():
    sin_hora = _revision(fecha_pago_detectada=None)  # llegó 09:02; ventana 08:02–09:07 local
    assert BankEmailService.motivo_coincidencia(CONFIG, sin_hora, _correo()) == "coincidencia_sin_referencia"
    tarde = _correo(fecha_correo=datetime(2026, 9, 30, 13, 30, 0))  # 07:30 local
    assert BankEmailService.motivo_coincidencia(CONFIG, sin_hora, tarde) == "fecha_hora_bancaria_fuera_de_ventana"


MARGARITA = SimpleNamespace(id=248, nombre="Margarita Moreno Lopez", cedula="D440")


class _DB:
    def __init__(self, correos, cliente=None):
        self.correos = correos
        self.cliente = cliente

    async def get(self, _modelo, _llave):
        return self.cliente

    async def execute(self, _consulta):
        correos = self.correos

        class _R:
            def scalars(self):
                return self

            def all(self):
                return correos

        return _R()


def _buscar(correos, revision=None, cliente=MARGARITA):
    servicio = BankEmailService()
    return asyncio.run(servicio._find_match_sin_referencia(
        _DB(correos, cliente), CONFIG, revision or _revision(cliente_id=248), Decimal("300.00")
    ))


def test_dos_depositos_iguales_sin_nada_del_cliente_van_a_revision_humana():
    dos = [_correo(concepto="Envio"), _correo(concepto="Envio", fecha_correo=datetime(2026, 9, 30, 15, 1, 0))]
    assert _buscar(dos) == (None, "multiples_correos_coincidentes")


def test_monto_y_hora_no_bastan_si_el_deposito_no_es_del_cliente():
    # Alguien sabe que otro cliente pagó $300 a esa hora y manda una captura editada.
    assert _buscar([_correo(concepto="fdeznet2E3A Folio: M01576668")]) == (None, "titular_no_coincide")


def test_caso_margarita_y_mauricio_se_toma_el_que_lleva_su_nombre():
    mauricio = _correo(concepto="fdeznet2E3A Folio: M01576668")
    margarita = _correo(concepto="margarita moreno lopez De la cuenta: BANORTE ***6634",
                        fecha_correo=datetime(2026, 9, 30, 14, 58, 19))
    assert _buscar([mauricio, margarita]) == (margarita, "coincidencia_sin_referencia")


def test_sin_cliente_identificado_vale_el_concepto_de_la_captura():
    margarita = _correo(concepto="margarita moreno lopez De la cuenta: BANORTE ***6634")
    revision = _revision(concepto_detectado="margarita moreno lopez")
    assert _buscar([margarita], revision, cliente=None) == (margarita, "coincidencia_sin_referencia")


def _senales(concepto, cliente=MARGARITA, **revision):
    return senales_de_titular(cliente, _revision(**revision), _correo(concepto=concepto))


def test_senales_del_titular():
    assert _senales("MARGARITA MORENO pago internet") == ["nombre"]
    assert _senales("pago de lopez") == []  # un solo apellido no basta
    mauricio = SimpleNamespace(nombre="Mauricio Balcazar Vazquez", cedula="2E3A")
    assert _senales("fdeznet2E3A Folio: M01576668", mauricio) == ["contrato"]
    assert _senales("Envio Folio: 2E3A123", mauricio) == []  # el contrato debe ir solo o tras letras
    assert _senales("pago fdeznet2e3a", None, concepto_detectado="fdeznet2E3A") == ["concepto"]
    assert _senales("pago internet", None, concepto_detectado="pago internet") == []  # palabras genéricas
    assert _senales("De la cuenta: BANORTE ***6634", None, cuentas_detectadas="5265,6634") == ["cuenta_origen"]


def test_la_bandeja_aprueba_a_mano_con_la_misma_regla_sin_referencia():
    import inspect
    from src.interfaces.api import whatsapp

    fuente = inspect.getsource(whatsapp.aprobar_comprobante_revision)
    assert "motivo_coincidencia(" in fuente and "COINCIDENCIAS_VALIDAS" in fuente


# --------------------------------------------- depósito elegido en la bandeja
def _elegido(revision=None, correo=None, **config):
    cfg = SimpleNamespace(**{**vars(CONFIG), **config})
    revision = revision or _revision(transaccion_correo_id=None)
    return BankEmailService.motivo_deposito_elegido(cfg, revision, correo or _correo(id=90, pago_id=None))


def test_la_persona_puede_elegir_un_deposito_valido_aunque_haya_varios():
    assert _elegido() == "deposito_elegido"


def test_el_deposito_elegido_debe_ser_autentico_libre_y_del_mismo_monto():
    assert _elegido(correo=_correo(id=90, pago_id=None, autenticado=False)) == "correo_bancario_no_autenticado"
    assert _elegido(correo=_correo(id=90, pago_id=None, monto=Decimal("350.00"))) == "monto_bancario_no_coincide"
    assert _elegido(correo=_correo(id=90, pago_id=None, cuenta_destino_terminacion="1111")) == "cuenta_destino_no_autorizada"
    assert _elegido(correo=_correo(id=90, pago_id=None, estado="conciliada")) == "correo_bancario_ya_utilizado"


def test_no_se_puede_elegir_el_deposito_apartado_para_otro_comprobante():
    apartado = _correo(id=89, pago_id=None, estado="reservada")
    assert _elegido(correo=apartado) == "correo_bancario_ya_utilizado"
    propio = _revision(transaccion_correo_id=89)
    assert _elegido(revision=propio, correo=apartado) == "deposito_elegido"


def test_si_la_captura_trae_folio_el_deposito_elegido_debe_tenerlo():
    con_folio = _revision(transaccion_correo_id=None, folio_detectado="OTRO999")
    assert _elegido(revision=con_folio) == "referencia_bancaria_no_coincide"


def test_el_deposito_elegido_debe_estar_en_los_dias_de_busqueda():
    viejo = _correo(id=90, pago_id=None, fecha_correo=datetime(2026, 9, 20, 10, 0))
    assert _elegido(correo=viejo) == "fecha_bancaria_fuera_de_ventana"


def test_la_lista_marca_cual_cuadra_por_hora_y_limpia_el_concepto(monkeypatch):
    servicio = BankEmailService()
    monkeypatch.setattr(servicio, "get_config", lambda _db: asyncio.sleep(0, CONFIG))
    mauricio = _correo(
        id=89, pago_id=None, fecha_correo=datetime(2026, 9, 30, 14, 54, 59), referencia="M01576668",
        concepto="fdeznet2E3A Folio: M01576668 Comisión (incluye IVA) $0 Contáctanos",
    )
    margarita = _correo(
        id=90, pago_id=None, cuenta_destino_terminacion="6342", fecha_correo=datetime(2026, 9, 30, 14, 58, 19),
        referencia="38432P0420", concepto="margarita moreno lopez De la cuenta: BANORTE ***6634",
    )
    lejano = _correo(id=70, pago_id=None, fecha_correo=datetime(2026, 9, 29, 23, 40, 36), concepto="Envio")
    depositos = asyncio.run(servicio.depositos_posibles(
        _DB([lejano, mauricio, margarita], MARGARITA), _revision(transaccion_correo_id=None, cliente_id=248)
    ))
    assert [d["id"] for d in depositos] == [90, 89, 70]  # primero el que lleva su nombre
    assert depositos[0]["titular"] == ["nombre"] and depositos[1]["titular"] == []
    assert depositos[1]["concepto"] == "fdeznet2E3A Folio: M01576668"
    assert depositos[0]["fecha"] == "2026-09-30T08:58:19"
    assert [d["coincide_hora"] for d in depositos] == [True, True, False]
