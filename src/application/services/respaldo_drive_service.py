"""Respaldo del ISP en su propio Google Drive (solo en la nube).

El respaldo lo hace el panel central; aquí solo se le piden las cosas firmadas
con NUBE_PANEL_SECRETO, que el panel le dio a esta instancia al crearla.
"""
import hashlib
import hmac
import os
import time
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException

VIGENCIA = 5 * 60


def _ajustes() -> tuple[str, str, str]:
    url = os.getenv("NUBE_PANEL_URL", "").strip().rstrip("/")
    isp = os.getenv("NUBE_ISP", "").strip()
    secreto = os.getenv("NUBE_PANEL_SECRETO", "").strip()
    if not (url and isp and secreto):
        raise HTTPException(status_code=404, detail="El respaldo en Google Drive no está disponible en esta instalación")
    return url, isp, secreto


def _firmados(accion: str) -> tuple[str, dict[str, str]]:
    url, isp, secreto = _ajustes()
    vence = int(time.time()) + VIGENCIA
    firma = hmac.new(secreto.encode(), f"{isp}:{accion}:{vence}".encode(), hashlib.sha256).hexdigest()
    return url, {"isp": isp, "vence": str(vence), "firma": firma}


def enlace_conectar(correo: str = "") -> str:
    """URL a la que va el navegador del admin para dar permiso en Google."""
    url, parametros = _firmados("conectar")
    if correo:
        parametros["correo"] = correo
    return f"{url}/drive/conectar?{urlencode(parametros)}"


async def _pedir(metodo: str, accion: str) -> dict:
    url, parametros = _firmados(accion)
    try:
        async with httpx.AsyncClient(timeout=30) as cliente:
            respuesta = await cliente.request(metodo, f"{url}/drive/{accion}", params=parametros)
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="No se pudo consultar el respaldo en Drive; intenta en un momento")
    if respuesta.status_code >= 400:
        try:
            detalle = respuesta.json().get("detail")
        except ValueError:
            detalle = None
        raise HTTPException(status_code=502 if respuesta.status_code >= 500 else respuesta.status_code,
                            detail=detalle or "El respaldo en Drive no respondió")
    return respuesta.json()


async def estado() -> dict:
    return await _pedir("GET", "estado")


async def desconectar() -> dict:
    return await _pedir("POST", "desconectar")


async def respaldar() -> dict:
    return await _pedir("POST", "respaldar")
