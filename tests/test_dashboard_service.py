import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.application.services.dashboard_service import DashboardService


class ResultadoFalso:
    def __init__(self, *, one=None, scalar=None, rows=None, scalars=None):
        self._one = one
        self._scalar = scalar
        self._rows = rows or []
        self._scalars = scalars or []

    def one(self):
        return self._one

    def scalar(self):
        return self._scalar

    def all(self):
        return self._rows

    def scalars(self):
        return ResultadoFalso(rows=self._scalars)


class BaseDatosFalsa:
    def __init__(self, resultados):
        self._resultados = iter(resultados)

    async def execute(self, _stmt):
        return next(self._resultados)


def test_home_distingue_historico_de_contratos_activos():
    resumen = SimpleNamespace(
        total=219,
        total_directorio=216,
        activos=204,
        suspendidos=12,
        servicios_actuales=216,
        total_servicios=219,
        pendientes_instalacion=3,
        cancelados=0,
        retirados=0,
        eliminados=0,
    )
    db = BaseDatosFalsa(
        [
            ResultadoFalso(one=resumen),
            ResultadoFalso(
                one=SimpleNamespace(
                    monto_cobrado=100,
                    clientes_cobrados=2,
                    pagos_recibidos=3,
                )
            ),
            ResultadoFalso(
                one=SimpleNamespace(
                    monto_cobrado=500,
                    clientes_cobrados=8,
                    pagos_recibidos=10,
                )
            ),
            ResultadoFalso(
                one=SimpleNamespace(
                    facturas_emitidas=216,
                    facturas_pagadas=180,
                    facturas_pendientes=30,
                    facturas_vencidas=6,
                    facturas_anuladas=1,
                    clientes_facturados=216,
                    servicios_facturados=216,
                    clientes_con_saldo_pendiente=36,
                    monto_facturado=21600,
                    saldo_pendiente=3600,
                )
            ),
        ]
    )
    servicio = DashboardService(db)
    servicio._obtener_ultimos_pagos = AsyncMock(return_value=[])

    respuesta = asyncio.run(servicio.obtener_home_data())
    clientes = respuesta["resumen_clientes"]

    assert clientes["total_registrados"] == 216
    assert clientes["total_clientes"] == 216
    assert clientes["total_historico"] == 219
    assert clientes["contratos_activos"] == 204
    assert clientes["contratos_no_activos"] == 12
    assert clientes["total_servicios_actuales"] == 216
    assert clientes["pendientes_instalacion"] == 3
    assert clientes["online_activos"] == 204
    assert respuesta["finanzas"] == {
        "cobrado_hoy": 100.0,
        "clientes_cobrados_hoy": 2,
        "pagos_recibidos_hoy": 3,
        "cobrado_mes": 500.0,
        "clientes_cobrados_mes": 8,
        "pagos_recibidos_mes": 10,
        "moneda": "MXN",
        "criterio": (
            "Solo pagos aplicados; no incluye anulados "
            "ni saldo a favor"
        ),
    }
    assert respuesta["facturacion"] == {
        "periodo_desde": respuesta["facturacion"]["periodo_desde"],
        "periodo_hasta": respuesta["facturacion"]["periodo_hasta"],
        "clientes_actuales": 216,
        "clientes_facturados": 216,
        "servicios_actuales": 216,
        "servicios_facturados": 216,
        "servicios_sin_factura": 0,
        "clientes_sin_factura": 0,
        "clientes_cobrados": 8,
        "clientes_con_saldo_pendiente": 36,
        "facturas_emitidas": 216,
        "facturas_pagadas": 180,
        "facturas_pendientes": 30,
        "facturas_vencidas": 6,
        "facturas_anuladas": 1,
        "monto_facturado": 21600.0,
        "monto_cobrado": 500.0,
        "saldo_pendiente": 3600.0,
        "porcentaje_cobertura": 100.0,
        "porcentaje_facturas_pagadas": 83.3,
        "total": 216,
        "pagadas": 180,
        "pendientes": 36,
        "porcentaje": 83.3,
    }


def test_metricas_concilian_online_mas_offline_con_activos():
    db = BaseDatosFalsa(
        [
            ResultadoFalso(scalars=[2]),
            ResultadoFalso(
                rows=[
                    (1, 10, "activo", True),
                    (2, 10, "activo", True),
                    (3, 20, "activo", False),
                    (4, 30, "suspendido", True),
                    (5, 40, "suspendido", False),
                ]
            ),
        ]
    )

    respuesta = asyncio.run(
        DashboardService(db).obtener_metricas_red()
    )
    metricas = respuesta["metricas"]
    conciliacion = respuesta["conciliacion"]

    assert metricas["total_clientes_directorio"] == 4
    assert metricas["total_servicios"] == 5
    assert metricas["clientes_online_mikrotik"] == 3
    assert metricas["clientes_offline_mikrotik"] == 2
    assert metricas["total_contratos_activos"] == 3
    assert metricas["contratos_activos_online"] == 2
    assert metricas["clientes_activos_sin_sesion"] == 1
    assert metricas["navegando_ok"] == 1
    assert metricas["morosos_online"] == 1
    assert metricas["falla_tecnica"] == 1
    assert metricas["contratos_no_activos"] == 2
    assert metricas["total_suspendidos"] == 2
    assert metricas["no_activos_online"] == 1
    assert metricas["no_activos_sin_sesion"] == 1
    assert conciliacion == {
        "total_clientes_directorio": 4,
        "total_servicios": 5,
        "online_mikrotik": 3,
        "sin_sesion": 2,
        "contratos_activos": 3,
        "activos_online_mikrotik": 2,
        "activos_sin_sesion": 1,
        "contratos_no_activos": 2,
        "no_activos_online": 1,
        "no_activos_sin_sesion": 1,
        "cuadra_directorio": True,
        "cuadra_contratos": True,
        "formula_directorio": (
            "total_servicios = online_mikrotik "
            "+ sin_sesion"
        ),
        "formula_contratos": (
            "contratos_activos = activos_online_mikrotik "
            "+ activos_sin_sesion"
        ),
    }


def test_moroso_offline_no_se_pierde_en_la_conciliacion():
    db = BaseDatosFalsa(
        [
            ResultadoFalso(scalars=[2]),
            ResultadoFalso(
                rows=[
                    (1, 10, "activo", True),
                    (2, 10, "activo", False),
                ]
            ),
        ]
    )

    respuesta = asyncio.run(
        DashboardService(db).obtener_metricas_red()
    )
    metricas = respuesta["metricas"]

    assert metricas["falla_tecnica"] == 0
    assert metricas["morosos_offline"] == 1
    assert metricas["clientes_activos_sin_sesion"] == 1
    assert respuesta["conciliacion"]["cuadra_contratos"] is True
