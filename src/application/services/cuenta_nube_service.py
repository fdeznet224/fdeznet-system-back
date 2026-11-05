"""Cuenta de WisPay del ISP (solo en la nube): aviso de pago, días que le quedan y versión.

El panel central sabe cuándo vence la mensualidad del ISP, cuándo lo cortará y
qué versión se liberó; aquí se le pregunta (firmado como el respaldo en Drive) y
se guarda unos minutos para no consultarlo en cada pantalla.
"""
import logging
import os
import time

import httpx
from fastapi import HTTPException

from src.application.services.respaldo_drive_service import _firmados

log = logging.getLogger(__name__)
VIGENCIA_CACHE = 10 * 60
_cache: dict = {"hasta": 0.0, "estado": None}


def en_la_nube() -> bool:
    return all(os.getenv(variable, "").strip() for variable in ("NUBE_PANEL_URL", "NUBE_ISP", "NUBE_PANEL_SECRETO"))


async def estado(fresco: bool = False) -> dict | None:
    """Lo que dice el panel de esta cuenta. Si no responde, se queda lo último que dijo."""
    if not en_la_nube():
        return None
    if not fresco and time.monotonic() < _cache["hasta"]:
        return _cache["estado"]
    url, parametros = _firmados("cuenta")
    try:
        async with httpx.AsyncClient(timeout=10) as cliente:
            respuesta = await cliente.get(f"{url}/cuenta/estado", params=parametros)
        respuesta.raise_for_status()
        _cache["estado"] = respuesta.json()
    except (httpx.HTTPError, ValueError) as error:
        log.warning("No se pudo consultar la cuenta en el panel: %s", error)
    _cache["hasta"] = time.monotonic() + VIGENCIA_CACHE
    return _cache["estado"]


async def aviso() -> dict | None:
    """{tono, mensaje, pagado_hasta, corte} o None."""
    datos = await estado()
    return (datos or {}).get("aviso")


async def actualizar() -> dict:
    """Pide al panel pasar este sistema a la versión liberada; el panel lo reinicia en segundo plano."""
    if not en_la_nube():
        raise HTTPException(status_code=404, detail="Solo disponible en la nube")
    url, parametros = _firmados("actualizar")
    try:
        async with httpx.AsyncClient(timeout=30) as cliente:
            respuesta = await cliente.post(f"{url}/cuenta/actualizar", params=parametros)
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="No se pudo contactar al panel; intenta en un momento")
    if respuesta.status_code >= 400:
        try:
            detalle = respuesta.json().get("detail")
        except ValueError:
            detalle = None
        raise HTTPException(status_code=502 if respuesta.status_code >= 500 else respuesta.status_code,
                            detail=detalle or "El panel no respondió")
    _cache["hasta"] = 0.0
    return respuesta.json()
