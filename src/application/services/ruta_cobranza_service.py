"""Ruta de cobranza: clientes con pagos vencidos, del más cercano al más lejano.

Para el cobrador en la calle. Con su ubicación se ordena por distancia al
domicilio (GPS del servicio); sin ubicación, por más días de atraso.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.application.services.nap_service import distancia_metros
from src.infrastructure.models import FacturaModel

ESTADOS_SIN_COBRO = ("pagada", "anulada", "consolidada", "sin_cargo")


def ordenar_ruta(morosos: list[dict], origen: tuple[float, float] | None) -> list[dict]:
    """Con origen: los que tienen GPS por cercanía y al final los que no."""
    if origen:
        for moroso in morosos:
            posicion = moroso.get("posicion")
            moroso["distancia_m"] = round(distancia_metros(origen, posicion)) if posicion else None
        return sorted(morosos, key=lambda m: (m["distancia_m"] is None, m["distancia_m"] or 0, -m["dias_atraso"]))
    return sorted(morosos, key=lambda m: (-m["dias_atraso"], -m["total"]))


async def ruta_cobranza(
    db: AsyncSession, latitud: float | None = None, longitud: float | None = None, hoy: date | None = None
) -> list[dict]:
    hoy = hoy or date.today()
    facturas = (
        await db.execute(
            select(FacturaModel)
            .options(selectinload(FacturaModel.cliente), selectinload(FacturaModel.servicio))
            .where(
                FacturaModel.saldo_pendiente > 0,
                FacturaModel.estado.notin_(ESTADOS_SIN_COBRO),
                FacturaModel.es_promesa_activa == False,  # noqa: E712
                FacturaModel.fecha_vencimiento < hoy,
            )
        )
    ).scalars().all()

    por_cliente: dict[int, dict] = {}
    for factura in facturas:
        cliente = factura.cliente
        if not cliente:
            continue
        servicio = factura.servicio
        lat = (servicio.latitud if servicio else None) or cliente.latitud
        lng = (servicio.longitud if servicio else None) or cliente.longitud
        moroso = por_cliente.setdefault(cliente.id, {
            "cliente_id": cliente.id,
            "nombre": cliente.nombre,
            "contrato": cliente.cedula,
            "telefono": cliente.telefono,
            "direccion": (servicio.direccion if servicio else None) or cliente.direccion,
            "estado": cliente.estado,
            "total": Decimal("0"),
            "dias_atraso": 0,
            "latitud": None,
            "longitud": None,
            "posicion": None,
        })
        moroso["total"] += Decimal(factura.saldo_pendiente or 0)
        moroso["dias_atraso"] = max(moroso["dias_atraso"], (hoy - factura.fecha_vencimiento).days)
        if moroso["posicion"] is None and lat and lng:
            moroso["latitud"], moroso["longitud"] = float(lat), float(lng)
            moroso["posicion"] = (float(lat), float(lng))

    origen = (latitud, longitud) if latitud is not None and longitud is not None else None
    ruta = ordenar_ruta(list(por_cliente.values()), origen)
    for moroso in ruta:
        moroso.pop("posicion")
        moroso["total"] = float(moroso["total"])
    return ruta
