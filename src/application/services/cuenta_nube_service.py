"""Aviso de pago de WisPay en el admin del ISP (solo en la nube).

El panel central sabe cuándo vence la mensualidad del ISP y cuándo lo cortará;
aquí se le pregunta (firmado como el respaldo en Drive) y se guarda unos minutos
para no consultarlo en cada pantalla.
"""
import logging
import os
import time

import httpx

from src.application.services.respaldo_drive_service import _firmados

log = logging.getLogger(__name__)
VIGENCIA_CACHE = 10 * 60
_cache: dict = {"hasta": 0.0, "aviso": None}


def en_la_nube() -> bool:
    return all(os.getenv(variable, "").strip() for variable in ("NUBE_PANEL_URL", "NUBE_ISP", "NUBE_PANEL_SECRETO"))


async def aviso() -> dict | None:
    """{tono, mensaje, pagado_hasta, corte} o None. Si el panel no responde, se queda lo último que dijo."""
    if not en_la_nube():
        return None
    if time.monotonic() < _cache["hasta"]:
        return _cache["aviso"]
    url, parametros = _firmados("cuenta")
    try:
        async with httpx.AsyncClient(timeout=10) as cliente:
            respuesta = await cliente.get(f"{url}/cuenta/estado", params=parametros)
        respuesta.raise_for_status()
        _cache["aviso"] = respuesta.json().get("aviso")
    except (httpx.HTTPError, ValueError) as error:
        log.warning("No se pudo consultar la cuenta en el panel: %s", error)
    _cache["hasta"] = time.monotonic() + VIGENCIA_CACHE
    return _cache["aviso"]
