"""Inicio del portal: falla en la zona del cliente y cómo va su reconexión tras un corte."""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

from src.application.services import billing_service
from src.application.services.falla_zona_service import FallaZonaService, Miembro, detectar
from src.application.services.portal_cliente_service import PortalClienteService

AHORA = datetime(2026, 10, 25, 15, 0)


def hace(minutos):
    return AHORA - timedelta(minutes=minutos)


def test_la_mayoria_de_la_caja_cayo_al_mismo_tiempo_es_falla():
    miembros = [Miembro(False, hace(30)), Miembro(False, hace(28)), Miembro(False, hace(27)), Miembro(True, hace(900))]
    falla = detectar(miembros, AHORA)
    assert falla == {"desde": hace(28), "afectados": 3}


def test_dos_caidos_no_son_falla_de_zona():
    assert detectar([Miembro(False, hace(10)), Miembro(False, hace(12)), Miembro(True, None)], AHORA) is None


def test_los_apagados_de_hace_dias_no_cuentan():
    # Tres casas vacías y dos conectadas: no hay falla hoy.
    miembros = [Miembro(False, hace(60 * 72))] * 3 + [Miembro(True, hace(60 * 50))] * 2
    assert detectar(miembros, AHORA) is None
    # Con tres caídos hoy, las casas vacías tampoco bajan la proporción.
    assert detectar(miembros + [Miembro(False, hace(20))] * 3, AHORA)["afectados"] == 3


def test_la_minoria_caida_no_es_falla():
    miembros = [Miembro(False, hace(10))] * 3 + [Miembro(True, None)] * 4
    assert detectar(miembros, AHORA) is None


def falla_con(grupos, **servicio):
    datos = {"id": 9, "estado": "activo", "is_online": False, "ultimo_cambio_estado": hace(20),
             "router_id": 1, "caja_nap_id": 5}
    datos.update(servicio)
    zona = FallaZonaService(db=None)

    async def del_campo(campo, valor):
        return grupos.get("nodo" if campo == "router_id" else "caja", [])

    async def de_la_troncal(servicio_id):
        return grupos.get("troncal", [])

    zona._del_campo, zona._de_la_troncal = del_campo, de_la_troncal
    cliente = SimpleNamespace(router_id=None, caja_nap_id=None)
    return asyncio.run(zona.para_servicio(SimpleNamespace(**datos), cliente, AHORA))


CAIDA = [Miembro(False, hace(25))] * 4
SANA = [Miembro(True, None)] * 10 + [Miembro(False, hace(25))]


def test_avisa_la_falla_mas_grande_que_lo_incluye():
    assert falla_con({"nodo": SANA, "troncal": CAIDA, "caja": CAIDA})["alcance"] == "troncal"
    assert falla_con({"nodo": SANA, "troncal": SANA, "caja": CAIDA})["alcance"] == "caja"
    assert falla_con({"nodo": SANA, "troncal": SANA, "caja": SANA}) is None


def test_sin_aviso_si_el_cliente_esta_conectado_o_suspendido():
    grupos = {"caja": CAIDA}
    assert falla_con(grupos, is_online=True) is None
    # Suspendido: está sin internet por el corte, no por la falla.
    assert falla_con(grupos, estado="suspendido") is None
    assert falla_con(grupos, ultimo_cambio_estado=hace(60 * 30)) is None


class Resultado:
    def __init__(self, valor):
        self.valor = valor

    def scalar_one_or_none(self):
        return self.valor


class BaseFalsa:
    """Responde en orden: primero el motivo de la suspensión, luego el comprobante."""

    def __init__(self, *respuestas):
        self.respuestas = list(respuestas)

    async def execute(self, _consulta):
        return Resultado(self.respuestas.pop(0))


def reconexion(servicio, *respuestas, deuda=False, monkeypatch):
    async def tiene_deuda(self, referencia):
        return deuda

    monkeypatch.setattr(billing_service.BillingService, "_servicio_tiene_deuda_pendiente", tiene_deuda)
    return asyncio.run(PortalClienteService(BaseFalsa(*respuestas))._reconexion(servicio))


def suspendido():
    return SimpleNamespace(id=9, cliente_id=3, estado="suspendido", is_online=False, ultima_reactivacion_en=None)


def test_reactivado_hace_poco_dice_si_ya_se_conecto(monkeypatch):
    servicio = SimpleNamespace(estado="activo", is_online=False, ultima_reactivacion_en=datetime.now() - timedelta(minutes=5))
    assert reconexion(servicio, monkeypatch=monkeypatch)["etapa"] == "reactivado"
    servicio.is_online = True
    assert reconexion(servicio, monkeypatch=monkeypatch)["conectado"] is True
    servicio.ultima_reactivacion_en = datetime.now() - timedelta(hours=3)
    assert reconexion(servicio, monkeypatch=monkeypatch) is None


def test_pago_aplicado_y_aun_suspendido_esta_reactivando(monkeypatch):
    assert reconexion(suspendido(), "falta_pago", monkeypatch=monkeypatch)["etapa"] == "reactivando"


def test_con_deuda_y_comprobante_en_revision(monkeypatch):
    recibido = datetime.now() - timedelta(minutes=8)
    resultado = reconexion(suspendido(), "falta_pago", recibido, deuda=True, monkeypatch=monkeypatch)
    assert resultado == {"etapa": "revisando_comprobante", "desde": recibido.isoformat(), "conectado": False}
    assert reconexion(suspendido(), "falta_pago", None, deuda=True, monkeypatch=monkeypatch) is None


def test_la_suspension_manual_no_promete_reconexion(monkeypatch):
    assert reconexion(suspendido(), "manual", monkeypatch=monkeypatch) is None


def test_la_app_del_cliente_se_instala_como_mi_internet_con_el_icono_del_isp(monkeypatch):
    from src.interfaces.api import configuracion

    marca = SimpleNamespace(empresa_nombre="FdezNet", color_primario="#2563eb", favicon_url=None)

    async def obtener(db):
        return marca

    monkeypatch.setattr(configuracion, "_obtener_configuracion", obtener)
    manifiesto = asyncio.run(configuracion.obtener_manifest_portal(db=None))
    assert manifiesto["name"] == manifiesto["short_name"] == "Mi Internet"
    assert manifiesto["icons"] == configuracion._iconos_pwa(marca)
