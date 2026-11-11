"""El técnico ve el prorrateo como pagado después de cobrarlo en efectivo (antes daba 500)."""
import asyncio
from datetime import date
from types import SimpleNamespace

from sqlalchemy.dialects import mysql

from src.application.services.activacion_service import ActivacionService


class DbFalsa:
    def __init__(self):
        self.consultas = []

    async def execute(self, consulta):
        # Que la consulta compile de verdad, como en MySQL.
        self.consultas.append(str(consulta.compile(dialect=mysql.dialect())))
        pago = SimpleNamespace(metodo_pago="efectivo")
        return SimpleNamespace(first=lambda: (pago, "Juan Técnico"))


def test_prorrateo_pagado_dice_quien_lo_cobro(monkeypatch):
    factura = SimpleNamespace(
        id=5, total=120, saldo_pendiente=0, periodo_desde=date(2026, 11, 10),
        periodo_hasta=date(2026, 11, 30), fecha_vencimiento=date(2026, 12, 1),
    )
    servicio = ActivacionService(DbFalsa())

    async def factura_falsa(_orden, _usuario):
        return factura

    monkeypatch.setattr(servicio, "_factura_prorrateo", factura_falsa)
    estado = asyncio.run(servicio.prorrateo(111, SimpleNamespace(id=1)))
    assert estado["pagado"] is True
    assert estado["metodo"] == "efectivo"
    assert estado["cobrado_por"] == "Juan Técnico"
    assert "nombre_completo" in servicio.db.consultas[0]
