"""Resumen de las OLT para Inicio: cuáles responden y cómo están las ONU de los clientes.

Usa las mismas lecturas que Radar (API de VSOL o SNMP), que ya se comparten
entre pantallas con el caché corto de cache_olt; además el resumen completo se
guarda un par de minutos porque Inicio se refresca cada pocos segundos.
"""

import asyncio
import logging
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.services.cache_olt import lectura_compartida
from src.application.services.senal_optica_service import UMBRAL_DEBIL_DBM
from src.application.services.snmp_service import SNMPMonitorService
from src.application.services.vsol_api_service import VsolApiService
from src.infrastructure.models import OLTModel

logger = logging.getLogger(__name__)

TTL_RESUMEN_S = 120
ESPERA_POR_OLT_S = 25


def potencia(rx_texto) -> float | None:
    """"-24.5 dBm" → -24.5; "LOS / Sin señal" → None."""
    encontrado = re.search(r"-?\d+(?:\.\d+)?", str(rx_texto or ""))
    return float(encontrado.group()) if encontrado else None


def resumir(lecturas: list[tuple[str, dict | None]]) -> dict:
    """lecturas: (nombre de la OLT, resultado de su monitoreo o None si no respondió)."""
    umbral = float(UMBRAL_DEBIL_DBM)
    en_linea, fuera, baja = set(), set(), set()
    olts_fuera = []
    sin_cliente = 0
    for nombre, datos in lecturas:
        if datos is None:
            olts_fuera.append(nombre)
            continue
        for cliente in datos.get("clientes_activos", []):
            en_linea.add(cliente["id_cliente"])
            rx = potencia(cliente.get("rx_power"))
            if rx is not None and rx < umbral:
                baja.add(cliente["id_cliente"])
        for cliente in datos.get("clientes_caidos", []):
            fuera.add(cliente["id_cliente"])
        sin_cliente += len(datos.get("onus_desconocidas", []))
    # Con dos domicilios, basta una ONU caída para que el cliente salga como caído.
    en_linea -= fuera
    return {
        "olts_en_linea": len(lecturas) - len(olts_fuera),
        "olts_fuera": len(olts_fuera),
        "olts_fuera_nombres": olts_fuera,
        "onu_en_linea": len(en_linea),
        "onu_fuera": len(fuera),
        "onu_potencia_baja": len(baja),
        "onu_sin_cliente": sin_cliente,
        "clientes_onu_fuera": sorted(fuera),
        "clientes_potencia_baja": sorted(baja),
    }


class MonitoreoOltService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def _monitorear(self, olt: OLTModel) -> dict | None:
        integracion = (olt.tipo_integracion or "snmp").strip().lower()
        try:
            if olt.api_enabled or integracion in {"vsol_api", "auto"}:
                lectura = VsolApiService(self.db).monitorear_olt_api(olt.id)
            else:
                lectura = SNMPMonitorService(self.db).monitorear_olt(olt.id)
            return await asyncio.wait_for(lectura, timeout=ESPERA_POR_OLT_S)
        except Exception as exc:  # una OLT caída no detiene a las demás
            logger.warning("Inicio: no se pudo leer la OLT %s: %s", olt.nombre, exc)
            return None

    async def _leer(self) -> dict:
        olts = (
            await self.db.execute(select(OLTModel).where(OLTModel.is_active == True).order_by(OLTModel.id))  # noqa: E712
        ).scalars().all()
        return resumir([(olt.nombre, await self._monitorear(olt)) for olt in olts])

    async def resumen(self) -> dict:
        return await lectura_compartida("inicio:olts", self._leer, ttl=TTL_RESUMEN_S)
