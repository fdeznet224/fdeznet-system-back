"""Escribe en la OLT el nombre del cliente como descripción de su ONU.

Así, entrando a la OLT, se ve de quién es cada ONU. Se compara con lo que la
OLT ya tiene y solo se escribe lo distinto: una ONU nueva o cambiada toma el
nombre en la siguiente pasada, y un nombre corregido en el sistema también.
"""

import asyncio
import logging
import re
import unicodedata

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.application.services.cache_olt import olvidar
from src.application.services.vsol_api_service import VsolApiService
from src.infrastructure.models import ClienteModel, OLTModel, ServicioModel

logger = logging.getLogger(__name__)

# Límite del panel VSOL (onu_description_max_length).
LARGO_MAXIMO = 64


def texto_descripcion(nombre, contrato=None) -> str:
    """"José Ñandú Pérez", "7659" → "Jose Nandu Perez 7659" (sin acentos, máx. 64)."""
    def limpio(texto):
        sin_acentos = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode()
        return " ".join(re.sub(r"[^A-Za-z0-9 ._-]", " ", sin_acentos).split())

    nombre, contrato = limpio(nombre), limpio(contrato)
    if not contrato:
        return nombre[:LARGO_MAXIMO].strip()
    # El contrato siempre cabe: con él la ONU se identifica sin duda.
    return f"{nombre[:LARGO_MAXIMO - len(contrato) - 1].strip()} {contrato}".strip()


def usa_api_vsol(olt) -> bool:
    """Mismo criterio que el Radar para leer la OLT por su API web."""
    tipo = str(getattr(olt, "tipo_integracion", None) or "vsol_api").lower()
    return bool(getattr(olt, "api_enabled", False)) or tipo in ("vsol_api", "auto")


class DescripcionOnuService:
    def __init__(self, db: AsyncSession, vsol: VsolApiService | None = None):
        self.db = db
        self.vsol = vsol or VsolApiService(db)

    async def _clientes_por_onu(self) -> dict:
        """Identificador de ONU → cliente (la de la ficha y la de cada servicio)."""
        resultado = {}
        clientes = (
            await self.db.execute(
                select(ClienteModel)
                .options(selectinload(ClienteModel.onu_asignada))
                .where(ClienteModel.onu_id.isnot(None), ClienteModel.estado != "cancelado")
            )
        ).scalars().all()
        for cliente in clientes:
            if cliente.onu_asignada and cliente.onu_asignada.identificador:
                resultado.setdefault(VsolApiService._normalizar_sn(cliente.onu_asignada.identificador), cliente)
        servicios = (
            await self.db.execute(
                select(ServicioModel)
                .options(selectinload(ServicioModel.onu), selectinload(ServicioModel.cliente))
                .where(ServicioModel.onu_id.isnot(None), ServicioModel.estado != "cancelado")
            )
        ).scalars().all()
        for servicio in servicios:
            if servicio.onu and servicio.onu.identificador and servicio.cliente:
                resultado.setdefault(VsolApiService._normalizar_sn(servicio.onu.identificador), servicio.cliente)
        return resultado

    async def cambios_pendientes(self, olt, clientes: dict) -> list[tuple[int, int, str, str]]:
        onus = (await self.vsol.listar_onus_unificadas(olt.id)).get("onus", [])
        cambios = []
        for onu in onus:
            serial = VsolApiService._normalizar_sn(onu.get("identificador"))
            cliente = clientes.get(serial)
            ubicacion = VsolApiService.ubicacion_onu(onu)
            if not cliente or not ubicacion:
                continue
            esperado = texto_descripcion(cliente.nombre, cliente.cedula)
            if esperado and str(onu.get("description") or "").strip() != esperado:
                cambios.append((ubicacion[0], ubicacion[1], serial, esperado))
        return cambios

    async def sincronizar(self, olt_ids=None, limite: int | None = None) -> dict:
        """Escribe los nombres que faltan o cambiaron; una OLT caída no frena a las demás."""
        consulta = select(OLTModel).order_by(OLTModel.id)
        if olt_ids:
            consulta = consulta.where(OLTModel.id.in_(list(olt_ids)))
        olts = [o for o in (await self.db.execute(consulta)).scalars().all() if usa_api_vsol(o)]
        clientes = await self._clientes_por_onu()
        reporte = {"escritas": 0, "errores": [], "olts": {}}
        for olt in olts:
            try:
                cambios = await self.cambios_pendientes(olt, clientes)
                if limite is not None:
                    cambios = cambios[:limite]
                if not cambios:
                    reporte["olts"][olt.nombre] = 0
                    continue
                resultado = await asyncio.to_thread(self.vsol._escribir_descripciones_sync, olt, cambios)
                olvidar(f"vsol:{olt.id}")
                reporte["olts"][olt.nombre] = resultado["escritas"]
                reporte["escritas"] += resultado["escritas"]
                reporte["errores"] += [f"{olt.nombre}: {e}" for e in resultado["errores"]]
                if resultado["escritas"] and resultado["guardada"] is False:
                    reporte["errores"].append(f"{olt.nombre}: no se pudo guardar la configuración")
            except Exception as exc:
                logger.warning("No se pudo sincronizar la OLT %s: %s", olt.nombre, exc)
                reporte["errores"].append(f"{olt.nombre}: {str(exc)[:150]}")
        return reporte
