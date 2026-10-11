"""Ruta de reparación: clientes con la potencia alta, del más cercano al técnico.

Si el técnico anda en una colonia y le sobra tiempo, aprovecha para arreglar
la fibra de quien la tiene alta antes de que el cliente reporte. Sale de la
última lectura automática de cada servicio (cada hora); las lecturas viejas
no cuentan porque ya no dicen cómo está la fibra.
"""

from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.services.nap_service import distancia_metros
from src.application.services.senal_optica_service import UMBRAL_DEBIL_DBM
from src.application.services.support_service import ESTADOS_ABIERTOS
from src.infrastructure.models import (
    CajaNapModel,
    ClienteModel,
    LecturaOpticaModel,
    OrdenServicioModel,
    ServicioModel,
    UsuarioModel,
)

# Abajo de esto ya conviene ir (al límite); abajo de UMBRAL_DEBIL_DBM falla.
UMBRAL_REVISAR_DBM = Decimal("-25")
LECTURA_VIGENTE = timedelta(days=3)
ESTADOS_CON_SERVICIO = ("activo",)
MAXIMO = 60


def ordenar_por_cercania(clientes: list[dict], origen: tuple[float, float] | None) -> list[dict]:
    """Con origen: los que tienen GPS por cercanía y al final los que no; sin origen, la peor potencia primero."""
    if origen:
        for cliente in clientes:
            posicion = cliente.get("posicion")
            cliente["distancia_m"] = round(distancia_metros(origen, posicion)) if posicion else None
        return sorted(clientes, key=lambda c: (c["distancia_m"] is None, c["distancia_m"] or 0, c["rx"]))
    return sorted(clientes, key=lambda c: c["rx"])


async def ruta_reparacion(
    db: AsyncSession, latitud: float | None = None, longitud: float | None = None, ahora: datetime | None = None
) -> list[dict]:
    desde = (ahora or datetime.now()) - LECTURA_VIGENTE
    ultima = (
        select(func.max(LecturaOpticaModel.id).label("id"))
        .where(LecturaOpticaModel.servicio_id.isnot(None))
        .group_by(LecturaOpticaModel.servicio_id)
        .subquery()
    )
    filas = (
        await db.execute(
            select(LecturaOpticaModel, ClienteModel, ServicioModel)
            .join(ultima, LecturaOpticaModel.id == ultima.c.id)
            .join(ClienteModel, ClienteModel.id == LecturaOpticaModel.cliente_id)
            .join(ServicioModel, ServicioModel.id == LecturaOpticaModel.servicio_id)
            .where(
                LecturaOpticaModel.potencia_rx_dbm < UMBRAL_REVISAR_DBM,
                LecturaOpticaModel.fecha >= desde,
                ClienteModel.estado.in_(ESTADOS_CON_SERVICIO),
                ServicioModel.estado.in_(ESTADOS_CON_SERVICIO),
            )
        )
    ).all()
    if not filas:
        return []

    clientes_ids = {cliente.id for _, cliente, _ in filas}
    abiertas = {
        orden.cliente_id: (orden, nombre)
        for orden, nombre in (
            await db.execute(
                select(OrdenServicioModel, func.coalesce(UsuarioModel.nombre_completo, UsuarioModel.usuario))
                .outerjoin(UsuarioModel, UsuarioModel.id == OrdenServicioModel.tecnico_id)
                .where(
                    OrdenServicioModel.cliente_id.in_(clientes_ids),
                    OrdenServicioModel.categoria_soporte.isnot(None),
                    OrdenServicioModel.estado.in_(ESTADOS_ABIERTOS),
                )
            )
        ).all()
    }
    cajas_ids = {(s.caja_nap_id or c.caja_nap_id) for _, c, s in filas} - {None}
    cajas = {
        caja.id: caja.nombre
        for caja in (
            await db.execute(select(CajaNapModel).where(CajaNapModel.id.in_(cajas_ids)))
        ).scalars().all()
    } if cajas_ids else {}

    resultado = []
    for lectura, cliente, servicio in filas:
        lat = servicio.latitud or cliente.latitud
        lng = servicio.longitud or cliente.longitud
        rx = Decimal(lectura.potencia_rx_dbm)
        abierta = abiertas.get(cliente.id)
        resultado.append({
            "cliente_id": cliente.id,
            "nombre": cliente.nombre,
            "contrato": cliente.cedula,
            "direccion": servicio.direccion or cliente.direccion,
            "rx": float(rx),
            "nivel": "alta" if rx < UMBRAL_DEBIL_DBM else "limite",
            "fecha_lectura": lectura.fecha.isoformat() if lectura.fecha else None,
            "nap": cajas.get(servicio.caja_nap_id or cliente.caja_nap_id),
            "puerto_nap": servicio.puerto_nap or cliente.puerto_nap,
            "latitud": float(lat) if lat else None,
            "longitud": float(lng) if lng else None,
            "posicion": (float(lat), float(lng)) if lat and lng else None,
            "reporte": (
                {"id": abierta[0].id, "estado": abierta[0].estado, "tecnico": abierta[1]}
                if abierta else None
            ),
        })

    origen = (latitud, longitud) if latitud is not None and longitud is not None else None
    ruta = ordenar_por_cercania(resultado, origen)[:MAXIMO]
    for cliente in ruta:
        cliente.pop("posicion")
    return ruta
