import asyncio
from datetime import date
from types import SimpleNamespace

from src.application.services import billing_service as billing_module
from src.application.services.billing_service import BillingService


class _ResultadoLista:
    def __init__(self, valores):
        self.valores = valores

    def unique(self):
        return self

    def scalars(self):
        return self

    def all(self):
        return self.valores

    def first(self):
        return self.valores[0] if self.valores else None


def test_baja_automatica_desactivada_no_consulta_nada():
    class DB:
        async def execute(self, _statement):
            raise AssertionError("no debe consultar")

    reporte = asyncio.run(BillingService(DB()).dar_baja_por_falta_de_pago(0))

    assert reporte == {"revisados": 0, "dados_de_baja": 0, "errores": 0}


def test_baja_automatica_cierra_dias_sin_servicio_y_crea_baja(monkeypatch):
    servicio = SimpleNamespace(id=9, cliente_id=11, estado="suspendido")
    admin = SimpleNamespace(id=1, rol="admin")
    respuestas = [_ResultadoLista([servicio]), _ResultadoLista([admin])]

    class DB:
        async def execute(self, _statement):
            return respuestas.pop(0)

    normalizadas = []
    bajas = []

    class Finanzas:
        def __init__(self, _db):
            pass

        async def normalizar_facturas_suspendidas(self, srv, fecha_reactivacion):
            normalizadas.append((srv.id, fecha_reactivacion))

    class Bajas:
        def __init__(self, _db):
            pass

        async def crear(self, **kwargs):
            bajas.append(kwargs)
            return SimpleNamespace(id=77)

        async def sincronizar_mikrotik(self, baja_id):
            bajas.append(("mikrotik", baja_id))

    import src.application.services.baja_service as baja_module

    monkeypatch.setattr(billing_module, "FinanceService", Finanzas)
    monkeypatch.setattr(baja_module, "BajaService", Bajas)

    reporte = asyncio.run(BillingService(DB()).dar_baja_por_falta_de_pago(90))

    assert reporte == {"revisados": 1, "dados_de_baja": 1, "errores": 0}
    assert normalizadas == [(9, date.max)]
    assert bajas[0]["servicio_id"] == 9
    assert bajas[0]["usuario"] is admin
    assert "90 días" in bajas[0]["motivo"]
    assert bajas[1] == ("mikrotik", 77)
