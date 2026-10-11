"""Una reparación (reporte de falla) se crea sin caja ni puerto sugeridos."""

import asyncio
from types import SimpleNamespace

from src.application.services.orden_service import OrdenService
from src.infrastructure.models import OrdenServicioModel


class _Lista:
    def __init__(self, valores):
        self.valores = valores

    def scalars(self):
        return self

    def all(self):
        return self.valores


class _DB:
    def __init__(self):
        self.agregados = []

    async def get(self, _modelo, _id):
        return SimpleNamespace(id=_id)

    async def execute(self, _consulta):
        return _Lista([SimpleNamespace(id=199)])

    def add(self, objeto):
        self.agregados.append(objeto)

    async def flush(self):
        for objeto in self.agregados:
            objeto.id = getattr(objeto, "id", None) or 41


def test_el_reporte_de_falla_crea_su_orden_sin_caja_sugerida():
    # Mismos datos que arma SupportService.crear_incidencia.
    datos = SimpleNamespace(
        tipo="reparacion", cliente_id=244, servicio_id=None, prospecto_nombre=None,
        prospecto_telefono=None, prospecto_direccion=None, tecnico_id=None, prioridad="alta",
        fecha_programada=None, motivo="sin_internet", descripcion="Subió la potencia de su fibra",
    )
    db = _DB()
    servicio = OrdenService(db)
    servicio.obtener = lambda orden_id: asyncio.sleep(0, orden_id)
    assert asyncio.run(servicio.crear(datos, SimpleNamespace(id=1), commit=False)) == 41
    orden = next(o for o in db.agregados if isinstance(o, OrdenServicioModel))
    assert (orden.tipo, orden.servicio_id, orden.estado) == ("reparacion", 199, "pendiente")
    assert orden.caja_nap_sugerida_id is None and orden.puerto_nap_sugerido is None


def test_el_inventario_acepta_onu_sin_modelo():
    # Las ONU EPON vinculadas por MAC desde la OLT por SNMP no traen modelo.
    from src.domain.schemas import ONURetorno

    onu = ONURetorno(id=1, identificador="4C:8D:53:E3:7D:10", tecnologia="EPON", modelo=None, estado="INSTALADO")
    assert onu.modelo is None


class _Res:
    def __init__(self, valor):
        self.valor = valor

    def scalar_one_or_none(self):
        return self.valor

    def scalars(self):
        return self

    def all(self):
        return self.valor


class _DBIncidencia:
    def __init__(self):
        # cliente, sus servicios, ¿incidencia duplicada?
        self.respuestas = [SimpleNamespace(id=244, estado="activo"), [SimpleNamespace(id=199)], None]
        self.commits = 0

    async def execute(self, _consulta):
        return _Res(self.respuestas.pop(0))

    async def commit(self):
        self.commits += 1


def _crear_incidencia(monkeypatch, tecnico_id):
    import src.application.services.support_service as soporte

    avisos = []

    async def crear(self, datos, usuario, commit=True):
        assert commit is False
        return SimpleNamespace(id=42, tecnico_id=datos.tecnico_id)

    async def avisar_cliente(db, orden, usuario_id):
        # Al cliente se le avisa siempre, ya con la categoría y el canal guardados.
        avisos.append(("cliente", orden.categoria_soporte, orden.canal_reporte))
        return True

    async def avisar(db, orden_id, asignado_por_id):
        assert db.commits == 1   # ya guardada
        avisos.append((orden_id, asignado_por_id))
        return True

    monkeypatch.setattr(soporte.OrdenService, "crear", crear)
    monkeypatch.setattr(soporte.OrdenService, "obtener", lambda self, orden_id, usuario=None: asyncio.sleep(0, orden_id))
    monkeypatch.setattr(soporte, "avisar_asignacion", avisar)
    monkeypatch.setattr(soporte, "avisar_cliente_reporte", avisar_cliente)
    asyncio.run(soporte.SupportService(_DBIncidencia()).crear_incidencia(
        cliente_id=244, categoria="potencia_baja", descripcion="Subió la potencia de su fibra",
        usuario=SimpleNamespace(id=1), tecnico_id=tecnico_id,
    ))
    return avisos


def test_el_reporte_con_tecnico_le_avisa_por_whatsapp(monkeypatch):
    assert _crear_incidencia(monkeypatch, tecnico_id=2) == [(42, 1), ("cliente", "potencia_baja", "panel")]


def test_el_reporte_sin_tecnico_no_avisa(monkeypatch):
    assert _crear_incidencia(monkeypatch, tecnico_id=None) == [("cliente", "potencia_baja", "panel")]
