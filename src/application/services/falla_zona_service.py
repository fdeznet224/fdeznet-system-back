"""Falla en la zona del cliente, para avisarle en su portal.

Se usa el estado PPPoE que el MikroTik reporta cada 3 minutos. Si el cliente
está sin conexión y con él cayeron la mayoría de los de su caja NAP, su
troncal (PON) o su nodo (MikroTik), la falla no es de su casa: el portal le
dice que ya se está atendiendo en lugar de que levante un reporte.

No cuenta a los suspendidos ni a los que llevan más de un día apagados (casas
vacías, módems desconectados): esos no dicen nada de la falla de hoy.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Optional

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.models import ClienteModel, LecturaOpticaModel, ServicioModel

VENTANA = timedelta(hours=24)
MINIMO_CAIDOS = 3
PROPORCION_CAIDOS = 0.6
# La troncal sale de la lectura de señal más reciente (se lee cada hora).
HORAS_LECTURA_PON = 48
# De mayor a menor: si cayó el nodo, no importa que también cayó la caja.
ALCANCES = ("nodo", "troncal", "caja")


@dataclass
class Miembro:
    online: bool
    cambio: Optional[datetime]


def detectar(miembros: Iterable[Miembro], ahora: datetime) -> Optional[dict]:
    """Cayeron al menos 3 y la mayoría de los que estaban conectados hace un día."""
    vigentes = [m for m in miembros if m.online or (m.cambio and ahora - m.cambio <= VENTANA)]
    caidos = [m for m in vigentes if not m.online]
    if len(caidos) < MINIMO_CAIDOS or len(caidos) < PROPORCION_CAIDOS * len(vigentes):
        return None
    # La falla empezó cuando cayó la mayoría, no con el primero que se desconectó.
    cambios = sorted(m.cambio for m in caidos if m.cambio)
    return {"desde": cambios[len(cambios) // 2] if cambios else None, "afectados": len(caidos)}


class FallaZonaService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def para_servicio(self, servicio: ServicioModel, cliente: ClienteModel, ahora: Optional[datetime] = None) -> Optional[dict]:
        ahora = ahora or datetime.now()
        if servicio.estado != "activo" or servicio.is_online:
            return None
        if servicio.ultimo_cambio_estado and ahora - servicio.ultimo_cambio_estado > VENTANA:
            return None
        grupos = {
            "nodo": await self._del_campo("router_id", servicio.router_id or cliente.router_id),
            "troncal": await self._de_la_troncal(servicio.id),
            "caja": await self._del_campo("caja_nap_id", servicio.caja_nap_id or cliente.caja_nap_id),
        }
        for alcance in ALCANCES:
            falla = detectar(grupos[alcance], ahora) if grupos[alcance] else None
            if falla:
                desde = falla["desde"]
                return {"alcance": alcance, "desde": desde.isoformat() if desde else None, "afectados": falla["afectados"]}
        return None

    async def _miembros(self, *condiciones) -> list[Miembro]:
        filas = (await self.db.execute(
            select(ServicioModel.is_online, ServicioModel.ultimo_cambio_estado)
            .join(ClienteModel, ClienteModel.id == ServicioModel.cliente_id)
            .where(ServicioModel.estado == "activo", ServicioModel.user_pppoe.isnot(None), *condiciones)
        )).all()
        return [Miembro(bool(online), cambio) for online, cambio in filas]

    async def _del_campo(self, campo: str, valor: Optional[int]) -> list[Miembro]:
        """Caja o nodo: el del servicio, o el del cliente si el servicio no lo trae."""
        if not valor:
            return []
        del_servicio, del_cliente = getattr(ServicioModel, campo), getattr(ClienteModel, campo)
        return await self._miembros(or_(del_servicio == valor, and_(del_servicio.is_(None), del_cliente == valor)))

    async def _de_la_troncal(self, servicio_id: int) -> list[Miembro]:
        desde = datetime.now() - timedelta(hours=HORAS_LECTURA_PON)
        ultima = (await self.db.execute(
            select(LecturaOpticaModel.olt_id, LecturaOpticaModel.pon)
            .where(LecturaOpticaModel.servicio_id == servicio_id, LecturaOpticaModel.fecha >= desde,
                   LecturaOpticaModel.olt_id.isnot(None), LecturaOpticaModel.pon.isnot(None))
            .order_by(LecturaOpticaModel.fecha.desc()).limit(1)
        )).first()
        if not ultima:
            return []
        vecinos = select(LecturaOpticaModel.servicio_id).where(
            LecturaOpticaModel.olt_id == ultima.olt_id, LecturaOpticaModel.pon == ultima.pon,
            LecturaOpticaModel.fecha >= desde, LecturaOpticaModel.servicio_id.isnot(None),
        ).distinct()
        return await self._miembros(ServicioModel.id.in_(vecinos))
