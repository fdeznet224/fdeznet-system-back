import asyncio
from types import SimpleNamespace

import pytest

from src.application.services import activacion_service as modulo
from src.application.services.activacion_service import (
    ActivacionService,
    ActivacionTecnicoRequest,
    usuario_pppoe_de,
)
from src.infrastructure.models import (
    ClienteModel,
    OrdenServicioModel,
    PlanModel,
    ZonaModel,
)


class _Resultado:
    def scalar_one_or_none(self):
        return None


class _DB:
    def __init__(self, objetos):
        self.objetos = objetos
        self.agregados = []
        self.commits = 0

    async def get(self, modelo, ident):
        return self.objetos.get((modelo, ident))

    async def execute(self, _consulta):
        return _Resultado()

    def add(self, objeto):
        self.agregados.append(objeto)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        return None


def _escenario(monkeypatch, plan_solicitado=11, tecnico_orden=7):
    orden = SimpleNamespace(
        id=40, tipo="instalacion", estado="asignada", version=3, cliente_id=None,
        tecnico_id=tecnico_orden, prospecto_nombre="Ana Lopez", prospecto_telefono="5550001111",
        prospecto_direccion="Calle 1", zona_id=2, plan_id=plan_solicitado, solucion=None,
    )
    zona = SimpleNamespace(id=2, nombre="Paraiso", router_id=3, olt_id=2, plantilla_id=2)
    planes = [SimpleNamespace(id=11, nombre="Plan 300", precio=300), SimpleNamespace(id=12, nombre="Plan 400", precio=400)]
    infra = {
        "router": SimpleNamespace(id=3, nombre="Paraiso", tipo_seguridad="pppoe"),
        "olt": SimpleNamespace(id=2, nombre="OLT Paraiso"),
        "planes": planes,
        "red": SimpleNamespace(id=5, nombre="Clientes", cidr="10.10.9.0/24"),
        "naps": [],
    }
    cliente = SimpleNamespace(id=90, onu_id=None, red_id=None, user_pppoe="Ana_Lopez", pass_pppoe="secreta")
    activado = SimpleNamespace(
        id=90, cedula="A7F2", nombre="Ana Lopez", user_pppoe="Ana_Lopez", pass_pppoe="secreta",
        ip_asignada="10.10.9.60", onu_asignada=SimpleNamespace(identificador="ZTEG00000001"), olt_id=2, onu_id=4,
    )
    db = _DB({
        (ZonaModel, 2): zona,
        (ClienteModel, 90): cliente,
        (OrdenServicioModel, 40): orden,
        (PlanModel, 11): planes[0],
        (PlanModel, 12): planes[1],
    })
    llamadas = {}

    async def registrar(self, datos, _tareas, usuario_operador=None, orden_solicitud_id=None):
        llamadas["registro"] = (datos, orden_solicitud_id)
        return SimpleNamespace(id=90)

    async def activar(self, cliente_id, datos, usuario_operador=None, orden_id=None):
        llamadas["activacion"] = (cliente_id, datos, orden_id)
        return activado

    async def solicitud(self, orden_id, usuario):
        if usuario.rol == "tecnico" and orden.tecnico_id != usuario.id:
            raise PermissionError("La solicitud está asignada a otro técnico")
        return orden

    async def infraestructura(self, _zona):
        return infra

    async def senal(self, _cliente_id):
        return {"potencia": "-19.5 dBm", "estado": "online", "recomendacion": "ok"}

    monkeypatch.setattr(modulo.ClientService, "registrar_cliente", registrar)
    monkeypatch.setattr(modulo.ClientService, "activar_instalacion", activar)
    monkeypatch.setattr(ActivacionService, "_solicitud", solicitud)
    monkeypatch.setattr(ActivacionService, "_infraestructura", infraestructura)
    monkeypatch.setattr(ActivacionService, "_senal", senal)
    return db, orden, llamadas


def _datos(**cambios):
    base = dict(
        version=3, nombre="Ana Lopez", telefono="5550001111", direccion="Calle 1 #20",
        zona_id=2, plan_id=11, contrato_apartado="a7f2", onu_id=4, caja_nap_id=8, puerto_nap=3,
        latitud=17.1, longitud=-93.2,
    )
    base.update(cambios)
    return ActivacionTecnicoRequest(**base)


TECNICO = SimpleNamespace(id=7, rol="tecnico")


def test_usuario_pppoe_como_en_el_panel():
    assert usuario_pppoe_de("José  Pérez Núñez") == "Jose_Perez_Nunez"


def test_el_tecnico_activa_la_solicitud_con_la_infraestructura_de_la_zona(monkeypatch):
    db, orden, llamadas = _escenario(monkeypatch)

    resultado = asyncio.run(ActivacionService(db).activar(40, _datos(meses_gratis=1), TECNICO))

    datos, solicitud_id = llamadas["registro"]
    assert solicitud_id == 40
    assert (datos.router_id, datos.olt_id, datos.plantilla_id, datos.red_id) == (3, 2, 2, 5)
    assert datos.contrato_apartado == "A7F2"
    assert datos.user_pppoe == "Ana_Lopez"
    assert datos.tecnico_id == 7
    cliente_id, activacion, orden_id = llamadas["activacion"]
    assert (cliente_id, orden_id, activacion.onu_id, activacion.plan_id) == (90, 40, 4, 11)
    assert resultado["contrato"] == "A7F2"
    assert resultado["password_pppoe"] == "secreta"
    assert resultado["senal"]["potencia"] == "-19.5 dBm"
    assert resultado["cambios"] == []
    assert activacion.meses_gratis == 1
    assert orden.solucion == "Instalado y activado por el técnico (1 mes gratis)"


def test_un_cambio_de_compania_no_lleva_mes_gratis(monkeypatch):
    db, orden, llamadas = _escenario(monkeypatch)

    resultado = asyncio.run(ActivacionService(db).activar(40, _datos(meses_gratis=0), TECNICO))

    _, activacion, _ = llamadas["activacion"]
    assert activacion.meses_gratis == 0
    assert resultado["meses_gratis"] == 0
    assert orden.solucion == "Instalado y activado por el técnico (sin mes gratis)"


def test_los_cambios_del_tecnico_quedan_anotados(monkeypatch):
    db, orden, _ = _escenario(monkeypatch)

    resultado = asyncio.run(
        ActivacionService(db).activar(40, _datos(plan_id=12, nombre="Ana María Lopez", plantilla_id=1), TECNICO)
    )

    assert any("plan Plan 300 → Plan 400" in c for c in resultado["cambios"])
    assert any("nombre" in c for c in resultado["cambios"])
    assert any("plantilla" in c for c in resultado["cambios"])
    assert "Cambios respecto a la solicitud" in orden.solucion
    assert any("Cambios del técnico" in getattr(h, "comentario", "") for h in db.agregados)


def test_el_plan_debe_ser_del_mikrotik_de_la_zona(monkeypatch):
    db, _, llamadas = _escenario(monkeypatch)
    with pytest.raises(ValueError, match="no pertenece"):
        asyncio.run(ActivacionService(db).activar(40, _datos(plan_id=99), TECNICO))
    assert "registro" not in llamadas


def test_la_solicitud_cambiada_en_otro_dispositivo_no_se_activa(monkeypatch):
    db, _, _ = _escenario(monkeypatch)
    with pytest.raises(RuntimeError):
        asyncio.run(ActivacionService(db).activar(40, _datos(version=2), TECNICO))


def test_otro_tecnico_no_puede_activar(monkeypatch):
    db, _, _ = _escenario(monkeypatch, tecnico_orden=8)
    with pytest.raises(PermissionError):
        asyncio.run(ActivacionService(db).activar(40, _datos(), TECNICO))


def test_reintento_reutiliza_el_cliente_ya_creado(monkeypatch):
    db, orden, llamadas = _escenario(monkeypatch)
    orden.cliente_id = 90

    asyncio.run(ActivacionService(db).activar(40, _datos(), TECNICO))

    assert "registro" not in llamadas
    assert llamadas["activacion"][0] == 90
