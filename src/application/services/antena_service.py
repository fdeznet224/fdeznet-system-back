"""Antena del cliente por radioenlace: ¿responde al ping desde su MikroTik?

Es el equivalente de "ONU en línea" para los clientes que no son de fibra.
Se guarda el último resultado en la ficha para mostrarlo en la tabla.
"""

import asyncio
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.mikrotik_service import MikroTikService
from src.infrastructure.models import ClienteModel, RouterModel

logger = logging.getLogger(__name__)

PINGS = 4
RE_TIEMPO = re.compile(r"(\d+(?:\.\d+)?)(ms|us|s)")


def milisegundos(texto: Any) -> Optional[float]:
    """RouterOS da "2ms613us", "14ms" o "500us"."""
    total, encontrado = 0.0, False
    for numero, unidad in RE_TIEMPO.findall(str(texto or "")):
        encontrado = True
        total += float(numero) * {"s": 1000.0, "ms": 1.0, "us": 0.001}[unidad]
    return round(total, 1) if encontrado else None


def resumir_ping(respuestas: Any, enviados: int = PINGS) -> Dict[str, Any]:
    tiempos = [t for t in (milisegundos(r.get("time")) for r in respuestas if isinstance(r, dict)) if t is not None] \
        if isinstance(respuestas, list) else []
    perdida = round(100 * (enviados - len(tiempos)) / enviados) if enviados else 100
    return {
        "responde": bool(tiempos),
        "latencia_ms": round(sum(tiempos) / len(tiempos), 1) if tiempos else None,
        "perdida": max(0, min(100, perdida)),
    }


def estado_antena(cliente: ClienteModel) -> Optional[Dict[str, Any]]:
    if getattr(cliente, "tipo_conexion", "fibra") != "radio":
        return None
    return {
        "ip": cliente.ip_antena,
        "responde": cliente.antena_responde,
        "latencia_ms": cliente.antena_latencia_ms,
        "perdida": cliente.antena_perdida,
        "fecha": cliente.antena_revisada_en.isoformat() if cliente.antena_revisada_en else None,
    }


class AntenaService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def _ping(self, router: RouterModel, ip: str) -> Dict[str, Any]:
        mk = MikroTikService(router.ip_vpn, router.user_api, router.pass_api, router.port_api)
        respuestas = await asyncio.to_thread(
            mk._request, "POST", "/ping", {"address": ip, "count": str(PINGS)}, True,
        )
        return resumir_ping(respuestas)

    async def _revisar(self, cliente: ClienteModel, routers: Dict[int, RouterModel]) -> Dict[str, Any]:
        router = routers.get(cliente.router_id) if cliente.router_id else None
        if router is None and cliente.router_id:
            router = await self.db.get(RouterModel, cliente.router_id)
        if not router:
            raise ValueError("El cliente no tiene MikroTik asignado.")
        resultado = await self._ping(router, cliente.ip_antena)
        cliente.antena_responde = resultado["responde"]
        cliente.antena_latencia_ms = resultado["latencia_ms"]
        cliente.antena_perdida = resultado["perdida"]
        cliente.antena_revisada_en = datetime.now()
        return resultado

    async def verificar(self, cliente_id: int) -> Dict[str, Any]:
        """Revisión al momento (Herramientas del cliente)."""
        cliente = await self.db.get(ClienteModel, cliente_id)
        if not cliente:
            raise ValueError("Cliente no encontrado.")
        if cliente.tipo_conexion != "radio":
            raise ValueError("El cliente es de fibra: revisa su ONU.")
        if not cliente.ip_antena:
            raise ValueError("Falta la IP de la antena en la ficha del cliente.")
        await self._revisar(cliente, {})
        await self.db.commit()
        return estado_antena(cliente)

    async def revisar_todas(self) -> Dict[str, int]:
        """Pasada automática: todas las antenas de clientes que no están cancelados."""
        clientes: List[ClienteModel] = (
            await self.db.execute(
                select(ClienteModel).where(
                    ClienteModel.tipo_conexion == "radio",
                    ClienteModel.ip_antena.isnot(None),
                    ClienteModel.estado != "cancelado",
                )
            )
        ).scalars().all()
        routers = {r.id: r for r in (await self.db.execute(select(RouterModel))).scalars().all()}
        conteo = {"revisadas": 0, "responden": 0, "errores": 0}
        for cliente in clientes:
            try:
                resultado = await self._revisar(cliente, routers)
                conteo["revisadas"] += 1
                conteo["responden"] += int(resultado["responde"])
            except Exception as exc:
                # Si el MikroTik no responde no se sabe nada de la antena: no se marca caída.
                conteo["errores"] += 1
                logger.warning("No se pudo revisar la antena de %s: %s", cliente.nombre, exc)
        await self.db.commit()
        return conteo
