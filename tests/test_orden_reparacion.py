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
