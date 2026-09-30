"""Capturas sin folio: se empatan con el correo por monto y hora cercana."""

import asyncio
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

from src.application.services.bank_email_service import BankEmailService

CONFIG = SimpleNamespace(activo=True, cuentas_destino_permitidas="6342, 5265", tolerancia_monto=Decimal("0"), ventana_dias=3)


def _revision(**cambios):
    datos = dict(
        folio_detectado=None, monto_detectado=Decimal("300.00"),
        fecha_pago_detectada=datetime(2026, 9, 30, 8, 58, 12),  # hora local de la captura
        fecha_recepcion=datetime(2026, 9, 30, 9, 2, 31),
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


class _DB:
    def __init__(self, correos):
        self.correos = correos

    async def execute(self, _consulta):
        correos = self.correos

        class _R:
            def scalars(self):
                return self

            def all(self):
                return correos

        return _R()


def test_dos_depositos_iguales_en_la_ventana_van_a_revision_humana():
    servicio = BankEmailService()
    dos = [_correo(), _correo(fecha_correo=datetime(2026, 9, 30, 15, 1, 0))]
    correo, motivo = asyncio.run(servicio._find_match_sin_referencia(_DB(dos), CONFIG, _revision(), Decimal("300.00")))
    assert correo is None and motivo == "multiples_correos_coincidentes"
    uno = [_correo()]
    correo, motivo = asyncio.run(servicio._find_match_sin_referencia(_DB(uno), CONFIG, _revision(), Decimal("300.00")))
    assert correo is uno[0] and motivo == "coincidencia_sin_referencia"


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
        _DB([lejano, mauricio, margarita]), _revision(transaccion_correo_id=None)
    ))
    assert [d["id"] for d in depositos] == [89, 90, 70]
    assert depositos[0]["concepto"] == "fdeznet2E3A Folio: M01576668"
    assert depositos[1]["fecha"] == "2026-09-30T08:58:19"
    assert [d["coincide_hora"] for d in depositos] == [True, True, False]
