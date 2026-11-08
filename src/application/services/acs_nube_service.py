"""ACS (TR-069) propio del ISP en la nube.

Cada ISP activa el suyo desde Infraestructura → TR-069: el panel central le crea
su GenieACS (puerto público, usuario y clave) y recrea esta API con
GENIEACS_NBI_URL, ACS_URL, ACS_USUARIO y ACS_CLAVE. Aquí solo se leen para que
el admin los copie al módem, y se le pide al panel que lo active.
"""
import logging
import os

import httpx
from fastapi import HTTPException

from src.application.services.cuenta_nube_service import en_la_nube
from src.application.services.respaldo_drive_service import _firmados

log = logging.getLogger(__name__)
INTERVALO_REPORTE = 300


def datos() -> dict:
    url = os.getenv("ACS_URL", "").strip()
    return {
        "en_la_nube": en_la_nube(),
        "activo": bool(url),
        "url": url or None,
        "usuario": os.getenv("ACS_USUARIO", "").strip() or None,
        "clave": os.getenv("ACS_CLAVE", "").strip() or None,
        "intervalo": INTERVALO_REPORTE,
    }


async def activar() -> dict:
    """Pide al panel crear el ACS; tarda 1 o 2 minutos y reinicia esta API."""
    if not en_la_nube():
        raise HTTPException(status_code=404, detail="Solo disponible en la nube")
    if datos()["activo"]:
        return {"ok": True}
    url, parametros = _firmados("acs")
    try:
        async with httpx.AsyncClient(timeout=30) as cliente:
            respuesta = await cliente.post(f"{url}/cuenta/acs", params=parametros)
    except httpx.HTTPError as error:
        log.warning("No se pudo pedir el ACS al panel: %s", error)
        raise HTTPException(status_code=502, detail="No se pudo contactar al panel; intenta en un momento")
    if respuesta.status_code >= 400:
        try:
            detalle = respuesta.json().get("detail")
        except ValueError:
            detalle = None
        raise HTTPException(status_code=502 if respuesta.status_code >= 500 else respuesta.status_code,
                            detail=detalle or "El panel no respondió")
    return {"ok": True}
