"""Un cliente dado de baja regresa con su mismo contrato y otra ONU."""

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


class _DB:
    def __init__(self, respuestas, objetos):
        self.respuestas = list(respuestas)
        self.objetos = objetos
        self.agregados = []
        self.commits = 0

    async def execute(self, _consulta):
        return _Resultado(self.respuestas.pop(0))

    async def get(self, modelo, id_):
        return self.objetos.get((modelo.__name__, id_))

    def add(self, objeto):
        self.agregados.append(objeto)

    async def flush(self):
        for objeto in self.agregados:
            objeto.id = getattr(objeto, "id", None) or 900

    async def commit(self):
        self.commits += 1


def _escenario(estado_baja="recuperada", onu_en_bodega=None, ip_ocupada=None, otro_dueno=None):
    cliente = SimpleNamespace(id=244, nombre="Keila Sanchez Bonifaz", olt_id=7, estado="cancelado")
    servicio = SimpleNamespace(
        id=199, cliente_id=244, estado="cancelado", ip_asignada=None, user_pppoe="Keila_Sanchez_Bonifaz",
        olt_id=None, onu_id=None, fecha_instalacion=date(2026, 3, 1), ciclo_facturacion="calendario",
        proxima_facturacion=None,
    )
    baja = SimpleNamespace(
        id=11, cliente_id=244, servicio_id=199, estado=estado_baja, ip_snapshot="10.10.10.27",
        caja_nap_id_snapshot=None, puerto_nap_snapshot=None, observaciones="Se fue de viaje", cancelada_en=None,
    )
    olt = SimpleNamespace(tecnologia="GPON")
    # ip ocupada, pppoe ocupado, ONU en inventario, otro cliente con esa ONU
    db = _DB([ip_ocupada, None, onu_en_bodega, otro_dueno],
             {("ClienteModel", 244): cliente, ("ServicioModel", 199): servicio, ("OLTModel", 7): olt})
    return db, baja, cliente, servicio


def _reactivar(monkeypatch, db, baja, identificador="hwtc0000aaaa"):
    asignadas = []

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

    servicio._obtener_bloqueada = bloqueada
    servicio._sincronizar_estado_cliente = estado_cliente
    servicio._sincronizar_legacy_si_principal = nada
    servicio.sincronizar_mikrotik = nada
    servicio.obtener = lambda *_a: asyncio.sleep(0, baja)
    asyncio.run(servicio.reactivar_con_onu(11, SimpleNamespace(id=1), identificador))
    return asignadas


def test_regresa_con_su_contrato_y_una_onu_nueva(monkeypatch):
    db, baja, cliente, servicio = _escenario()
    asignadas = _reactivar(monkeypatch, db, baja)
    nueva = db.agregados[0]
    assert (nueva.identificador, nueva.tecnologia, nueva.estado) == ("HWTC0000AAAA", "GPON", "DISPONIBLE")
    assert asignadas == [(199, nueva.id, "Reactivación del cliente")]
    assert servicio.estado == "activo" and cliente.estado == "activo"
    assert servicio.ip_asignada == "10.10.10.27" and servicio.olt_id == 7
    # El cobro vuelve a empezar desde hoy, no desde antes del viaje.
    assert servicio.proxima_facturacion == date.today() == servicio.fecha_inicio_cobro
    assert baja.estado == "reactivada" and "con la ONU HWTC0000AAAA" in baja.observaciones
    assert db.commits == 1


def test_usa_su_onu_si_volvio_a_bodega(monkeypatch):
    suya = SimpleNamespace(id=122, identificador="ZTEG25525651", estado="DISPONIBLE")
    db, baja, _, _ = _escenario(onu_en_bodega=suya)
    asignadas = _reactivar(monkeypatch, db, baja, "ZTEG25525651")
    assert asignadas[0][1] == 122 and db.agregados == []


def test_no_reactiva_si_su_ip_ya_es_de_otro(monkeypatch):
    db, baja, _, servicio = _escenario(ip_ocupada=(5,))
    with pytest.raises(ValueError, match="10.10.10.27 ya la usa otro servicio"):
        _reactivar(monkeypatch, db, baja)
    assert servicio.estado == "cancelado"


def test_no_usa_una_onu_de_otro_cliente(monkeypatch):
    ajena = SimpleNamespace(id=50, identificador="HWTC0000AAAA", estado="DISPONIBLE")
    db, baja, _, servicio = _escenario(onu_en_bodega=ajena, otro_dueno="Ana Gloria")
    with pytest.raises(ValueError, match="ya es de Ana Gloria"):
        _reactivar(monkeypatch, db, baja)
    assert servicio.estado == "cancelado"


def test_si_la_onu_sigue_en_su_casa_se_revierte_la_baja(monkeypatch):
    db, baja, _, _ = _escenario(estado_baja="pendiente_retiro")
    with pytest.raises(ValueError, match="revierte la baja"):
        _reactivar(monkeypatch, db, baja)
