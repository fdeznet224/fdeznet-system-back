"""Fichas de la red «Zona FdezNet» vendidas desde el portal del cliente.

El portal entra al sistema de fichas (FdezNet Digital) como un vendedor más:
pide la cotización y la venta de una ficha y recibe su código. Se configura
en el .env:
  FICHAS_API_URL  (por omisión http://127.0.0.1:8200/api/v1)
  FICHAS_USUARIO / FICHAS_CLAVE  (un vendedor creado para el portal)
Sin vendedor configurado el portal no ofrece fichas.
"""

import os
import uuid
from typing import Any, Dict, List, Optional

import httpx

API_PREDETERMINADA = "http://127.0.0.1:8200/api/v1"


def configuracion() -> Optional[Dict[str, str]]:
    usuario, clave = os.getenv("FICHAS_USUARIO", "").strip(), os.getenv("FICHAS_CLAVE", "").strip()
    if not usuario or not clave:
        return None
    return {"url": (os.getenv("FICHAS_API_URL", "").strip() or API_PREDETERMINADA).rstrip("/"), "usuario": usuario, "clave": clave}


class FichasService:
    def __init__(self, config: Dict[str, str], http: Optional[httpx.AsyncClient] = None):
        self.config = config
        self.http = http

    async def __aenter__(self) -> "FichasService":
        if not self.http:
            self.http = httpx.AsyncClient(base_url=self.config["url"], timeout=30)
            self._propio = True
        respuesta = await self.http.post("/auth/token", data={"username": self.config["usuario"], "password": self.config["clave"]})
        if respuesta.status_code != 200:
            raise ValueError("El sistema de fichas no aceptó al vendedor del portal.")
        self.http.headers["Authorization"] = f"Bearer {respuesta.json()['access_token']}"
        return self

    async def __aexit__(self, *_):
        if getattr(self, "_propio", False):
            await self.http.aclose()

    async def perfiles(self) -> List[Dict[str, Any]]:
        respuesta = await self.http.get("/seller/profiles")
        respuesta.raise_for_status()
        return [{"id": p["id"], "nombre": p["name"], "precio": float(p["public_price"]), "duracion": p.get("session_timeout")}
                for p in respuesta.json()]

    async def vender(self, perfil_id: str) -> Dict[str, Any]:
        """Una ficha: cotiza y vende con la misma llave, para no duplicar si se reintenta."""
        cotizacion = await self.http.post("/seller/sales/preview", json={
            "profile_id": perfil_id, "quantity": 1, "request_key": uuid.uuid4().hex,
        })
        if cotizacion.status_code >= 400:
            raise ValueError("Esa ficha no está disponible ahora.")
        cotizacion = cotizacion.json()
        venta = await self.http.post("/seller/sales", json={
            "profile_id": perfil_id, "quantity": 1, "quote_token": cotizacion["quote_token"],
        })
        if venta.status_code >= 400:
            raise ValueError("El sistema de fichas no pudo generar la ficha; intenta en un momento.")
        venta = venta.json()
        fichas = venta.get("vouchers") or []
        if not fichas or not fichas[0].get("code"):
            raise ValueError(venta.get("error_detail") or "El sistema de fichas no entregó el código.")
        ficha = fichas[0]
        return {
            "folio": venta.get("folio"),
            "perfil": venta.get("profile_name") or cotizacion.get("profile_name"),
            "precio": float(venta.get("gross_total") or cotizacion.get("gross_total") or venta.get("unit_price") or 0),
            "codigo": ficha["code"],
            "clave": ficha.get("password"),
            "duracion": ficha.get("session_timeout"),
            "login_url": ficha.get("login_url"),
        }
