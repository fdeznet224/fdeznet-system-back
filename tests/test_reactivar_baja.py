"""Botón "Activar" de un cliente cancelado: mismo contrato, su ONU u otra."""

import asyncio
from datetime import date
from types import SimpleNamespace

import pytest

import src.application.services.baja_service as modulo
from src.application.services.baja_service import BajaService


class _Resultado:
    def __init__(self, valor):
        self.valor = valor

    def first(self):
        return self.valor

    def scalar_one_or_none(self):
        return self.valor

    def scalars(self):
        return self


class _DB:
    def __init__(self, respuestas, objetos):
        self.respuestas = list(respuestas)
        self.objetos = objetos
        self.commits = 0

    async def execute(self, _consulta):
        return _Resultado(self.respuestas.pop(0))

    async def get(self, modelo, id_):
        return self.objetos.get((modelo.__name__, id_))

    async def commit(self):
        self.commits += 1


ONU_NUEVA = SimpleNamespace(id=300, identificador="HWTC0000AAAA", estado="DISPONIBLE")


def _escenario(estado_baja="recuperada", respuestas=None, onu=ONU_NUEVA):
    cliente = SimpleNamespace(id=244, nombre="Keila Sanchez Bonifaz", olt_id=7, estado="cancelado")
    servicio = SimpleNamespace(
        id=199, cliente_id=244, estado="cancelado", ip_asignada=None, user_pppoe="Keila_Sanchez_Bonifaz",
        olt_id=None, onu_id=None, fecha_instalacion=date(2026, 3, 1), ciclo_facturacion="calendario",
        proxima_facturacion=None,
    )
    baja = SimpleNamespace(
        id=11, cliente_id=244, servicio_id=199, onu_id=122, estado=estado_baja, ip_snapshot="10.10.10.27",
        caja_nap_id_snapshot=None, puerto_nap_snapshot=None, observaciones="Se fue de viaje", cancelada_en=None,
    )
    objetos = {("ClienteModel", 244): cliente, ("ServicioModel", 199): servicio, ("InventarioONUModel", onu.id): onu}
    # baja del cliente, y luego lo que pida cada caso
    return _DB([baja] + (respuestas or []), objetos), baja, cliente, servicio


def _activar(monkeypatch, db, baja, onu_id=None):
    asignadas, revertidas = [], []

    async def asignar_onu_servicio(self, servicio, onu_id, usuario_id, motivo=""):
        asignadas.append((servicio.id, onu_id, motivo))
        servicio.onu_id = onu_id

    monkeypatch.setattr(modulo.FTTHService, "asignar_onu_servicio", asignar_onu_servicio)
    servicio = BajaService(db)

    async def bloqueada(_id):
        return baja

    async def nada(*_a, **_k):
        return None

    async def estado_cliente(cliente):
        cliente.estado = "activo"

    async def revertir(baja_id, _usuario):
        revertidas.append(baja_id)
        return baja

    servicio._obtener_bloqueada = bloqueada
    servicio._sincronizar_estado_cliente = estado_cliente
    servicio._sincronizar_legacy_si_principal = nada
    servicio.sincronizar_mikrotik = nada
    servicio.cancelar_y_reactivar = revertir
    servicio.obtener = lambda *_a: asyncio.sleep(0, baja)
    asyncio.run(servicio.reactivar_cliente(244, SimpleNamespace(id=1), onu_id))
    return asignadas, revertidas


def test_onu_recogida_se_activa_con_la_onu_elegida(monkeypatch):
    # IP libre, PPPoE libre, nadie más tiene la ONU
    db, baja, cliente, servicio = _escenario(respuestas=[None, None, None])
    asignadas, revertidas = _activar(monkeypatch, db, baja, onu_id=300)
    assert revertidas == []
    assert asignadas == [(199, 300, "Reactivación del cliente")]
    assert servicio.estado == "activo" and cliente.estado == "activo"
    assert servicio.ip_asignada == "10.10.10.27" and servicio.olt_id == 7
    # El cobro vuelve a empezar desde hoy, no desde antes de la baja.
    assert servicio.proxima_facturacion == date.today() == servicio.fecha_inicio_cobro
    assert baja.estado == "reactivada" and "con la ONU HWTC0000AAAA" in baja.observaciones


def test_onu_recogida_sin_elegir_onu_lo_pide():
    db, baja, _, _ = _escenario()
    with pytest.raises(ValueError, match="elige la ONU"):
        asyncio.run(BajaService(db).reactivar_cliente(244, SimpleNamespace(id=1), None))


def test_no_activa_si_su_ip_ya_es_de_otro(monkeypatch):
    db, baja, _, servicio = _escenario(respuestas=[(5,)])
    with pytest.raises(ValueError, match="10.10.10.27 ya la usa otro servicio"):
        _activar(monkeypatch, db, baja, onu_id=300)
    assert servicio.estado == "cancelado"


def test_no_usa_una_onu_de_otro_cliente(monkeypatch):
    db, baja, _, servicio = _escenario(respuestas=[None, None, "Ana Gloria"])
    with pytest.raises(ValueError, match="ya es de Ana Gloria"):
        _activar(monkeypatch, db, baja, onu_id=300)
    assert servicio.estado == "cancelado"


def test_su_onu_sigue_en_casa_se_revierte_la_baja(monkeypatch):
    db, baja, _, _ = _escenario(estado_baja="pendiente_retiro")
    asignadas, revertidas = _activar(monkeypatch, db, baja)
    assert revertidas == [11] and asignadas == []


def test_su_onu_sigue_en_casa_pero_elige_otra_la_anterior_se_desvincula(monkeypatch):
    db, baja, _, servicio = _escenario(estado_baja="pendiente_retiro", respuestas=[None])
    asignadas, revertidas = _activar(monkeypatch, db, baja, onu_id=300)
    assert revertidas == [11]
    # asignar_onu_servicio deja la anterior en bodega y la nueva en el servicio.
    assert asignadas == [(199, 300, "Reactivación con otra ONU")] and servicio.onu_id == 300


def test_otra_onu_ocupada_no_revierte_nada(monkeypatch):
    ocupada = SimpleNamespace(id=300, identificador="HWTC0000AAAA", estado="INSTALADO")
    db, baja, _, _ = _escenario(estado_baja="pendiente_retiro", onu=ocupada)
    with pytest.raises(ValueError, match="no está disponible"):
        _activar(monkeypatch, db, baja, onu_id=300)
