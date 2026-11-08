"""Llamar a la ONU que dejó de reportarse al ACS.

Una Huawei que arranca tras un apagón puede quedarse sin reportarse días aunque siga
dando internet: su primer aviso al ACS falla y ya no reintenta (le pasó a una HG8145V5
el 29 oct 2026, diez días). Un "connection request" desde el ACS la destraba.

Cada vuelta: a los equipos de clientes activos que llevan más de 1 h sin reportarse
(y menos de 30 días) se les pide una lectura mínima (su UpTime) con aviso. Si contesta,
vuelve a reportarse sola; si no (apagado o sin internet), se quita la orden de la cola.
A cada equipo se le llama a lo más una vez por hora.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.services.config_onu_service import ficha_equipo
from src.application.services.plantilla_tr069_service import cliente_de, clientes_por_equipo
from src.application.services.red_cliente_service import ESPERA_ONU_MS, RedClienteService
from src.infrastructure.models import ClienteModel, LogCronjobModel

logger = logging.getLogger(__name__)

DORMIDO = timedelta(hours=1)
MAXIMO = timedelta(days=30)
ESPERA = timedelta(hours=1)
LECTURA = {"name": "getParameterValues", "parameterNames": ["InternetGatewayDevice.DeviceInfo.UpTime"]}
_ultimo_llamado: Dict[str, datetime] = {}


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def _iso(fecha: datetime) -> str:
    return fecha.strftime("%Y-%m-%dT%H:%M:%S.000Z")


class DespertarTr069Service:
    def __init__(self, db: AsyncSession, nbi: Optional[str] = None, transporte=None):
        self.db = db
        self.red = RedClienteService(db, nbi)
        self.transporte = transporte

    async def _activos(self) -> set:
        filas = await self.db.execute(select(ClienteModel.id).where(ClienteModel.estado == "activo"))
        return {fila[0] for fila in filas.all()}

    async def revisar(self) -> Dict[str, int]:
        conteo = {"llamados": 0, "contestaron": 0}
        ahora = _ahora()
        async with httpx.AsyncClient(base_url=self.red.nbi, timeout=ESPERA_ONU_MS / 1000 + 10, transport=self.transporte) as http:
            try:
                respuesta = await http.get("/devices", params={
                    "query": json.dumps({"_lastInform": {"$lt": _iso(ahora - DORMIDO), "$gt": _iso(ahora - MAXIMO)}}),
                    "projection": "_id,_lastInform,_deviceId,InternetGatewayDevice.ManagementServer.ConnectionRequestURL",
                })
                respuesta.raise_for_status()
            except httpx.HTTPError as error:
                # Sin ACS (un ISP que no lo ha activado) o caído: no hay a quién llamar.
                logger.debug("Sin ACS para llamar a las ONU: %s", error)
                return conteo
            dormidos = respuesta.json()
            if not dormidos:
                return conteo
            mapas = await clientes_por_equipo(self.db)
            activos = await self._activos()
            for documento in dormidos:
                device_id = documento["_id"]
                cliente = cliente_de(documento, mapas)
                if not cliente or cliente["id"] not in activos:
                    continue
                anterior = _ultimo_llamado.get(device_id)
                if anterior and ahora - anterior < ESPERA:
                    continue
                _ultimo_llamado[device_id] = ahora
                conteo["llamados"] += 1
                try:
                    contesto = await self.red._tarea(http, device_id, LECTURA)
                except (ValueError, httpx.HTTPError):
                    contesto = False
                if not contesto:
                    await self._quitar_de_la_cola(http, device_id)
                else:
                    conteo["contestaron"] += 1
                ficha = ficha_equipo(documento)
                horas = round((ahora - datetime.fromisoformat(documento["_lastInform"].replace("Z", "+00:00"))).total_seconds() / 3600)
                mensaje = (f"TR-069: {ficha['serie']} de {cliente['nombre']} llevaba {horas} h sin reportarse; "
                           + ("se le llamó y contestó (vuelve a reportarse)" if contesto else "se le llamó y no contestó (apagado o sin internet)"))
                logger.info(mensaje)
                self.db.add(LogCronjobModel(nivel="INFO" if contesto else "WARN", origen="TR-069", mensaje=mensaje[:500]))
                await self.db.commit()
        return conteo

    async def _quitar_de_la_cola(self, http: httpx.AsyncClient, device_id: str) -> None:
        """Para que la lectura no se quede esperando y se le junte otra en la próxima vuelta."""
        try:
            pendientes = (await http.get("/tasks", params={"query": json.dumps({"device": device_id})})).json()
            for tarea in pendientes:
                if tarea.get("name") == LECTURA["name"]:
                    await http.delete(f"/tasks/{tarea['_id']}")
        except httpx.HTTPError as error:
            logger.warning("No se pudo limpiar la cola de %s: %s", device_id, error)
