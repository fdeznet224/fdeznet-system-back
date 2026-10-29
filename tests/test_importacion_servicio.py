"""Clientes del Excel de importación: nacen con su servicio activo."""
import asyncio
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.application.services.importacion_service import (
    crear_servicio_importado,
    proximo_dia_de_pago,
)


def test_proximo_dia_de_pago():
    assert proximo_dia_de_pago(date(2026, 10, 28), 5) == date(2026, 11, 5)
    assert proximo_dia_de_pago(date(2026, 10, 3), 5) == date(2026, 10, 5)
    assert proximo_dia_de_pago(date(2026, 10, 5), 5) == date(2026, 10, 5)
    assert proximo_dia_de_pago(date(2027, 1, 31), 30) == date(2027, 2, 28)
    assert proximo_dia_de_pago(date(2026, 10, 28), None) == date(2026, 10, 28)


def test_servicio_importado_queda_activo_con_su_red_y_cobro_en_dia_de_pago():
    plantilla = SimpleNamespace(dia_pago=5, dias_tolerancia=3, ciclo_facturacion="calendario")
    db = MagicMock(get=AsyncMock(return_value=plantilla), flush=AsyncMock())
    cliente = SimpleNamespace(
        id=7, direccion="Centro", latitud=None, longitud=None, router_id=1, plan_id=2,
        plantilla_id=4, zona_id=3, red_id=None, olt_id=None, caja_nap_id=None, puerto_nap=None,
        onu_id=None, ip_asignada="10.10.10.20", mac_address=None, user_pppoe="juan",
        pass_pppoe="x",
    )
    servicio = asyncio.run(crear_servicio_importado(db, cliente, hoy=date(2026, 10, 28)))

    assert servicio.estado == "activo"
    assert (servicio.router_id, servicio.plan_id, servicio.user_pppoe) == (1, 2, "juan")
    assert servicio.ip_asignada == "10.10.10.20"
    assert servicio.proxima_facturacion == date(2026, 11, 5)
    assert servicio.dias_tolerancia == 3
    db.add.assert_called_once_with(servicio)
