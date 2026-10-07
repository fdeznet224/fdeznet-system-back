"""Leer el SN de una ONU Huawei entrando a su página web por la IP del cliente.

Sirve para saber de quién es cada ONU que la OLT reporta sin cliente: el
sistema conoce la IP que le da al cliente y, desde el VPS, esa IP llega a la
ONU. Las credenciales salen de ONU_WEB_CREDENCIALES ("usuario:clave,...") para
que cada ISP ponga las suyas; por omisión se prueban las de fábrica.
"""

import base64
import logging
import os
import re

import httpx

logger = logging.getLogger(__name__)

CREDENCIALES_DE_FABRICA = "telecomadmin:admintelecom,root:admin"
PAGINAS_DE_INFORMACION = (
    "/html/ssmp/deviceinfo/deviceinfo.asp",
    "/html/status/deviceinfo.asp",
)
# Un SN GPON va como "HWTC1A2B3C4D" o en hexadecimal "485754431A2B3C4D".
RE_SN_TEXTO = re.compile(r"\b([A-Z]{4}[0-9A-F]{8})\b")
RE_SN_HEX = re.compile(r"\b([0-9A-Fa-f]{16})\b")


def credenciales() -> list[tuple[str, str]]:
    texto = os.getenv("ONU_WEB_CREDENCIALES", "").strip() or CREDENCIALES_DE_FABRICA
    pares = []
    for par in texto.split(","):
        usuario, _, clave = par.strip().partition(":")
        if usuario:
            pares.append((usuario, clave))
    return pares


def sn_de_hex(hexadecimal: str) -> str | None:
    """"485754431A2B3C4D" → "HWTC1A2B3C4D" (los 4 primeros bytes son el fabricante)."""
    try:
        fabricante = bytes.fromhex(hexadecimal[:8]).decode("ascii")
    except (ValueError, UnicodeDecodeError):
        return None
    if not re.fullmatch(r"[A-Z]{4}", fabricante):
        return None
    return fabricante + hexadecimal[8:].upper()


def seriales_en_pagina(html: str) -> list[str]:
    """Todos los SN que aparecen en la página de información del equipo."""
    encontrados = []
    for hexadecimal in RE_SN_HEX.findall(html or ""):
        sn = sn_de_hex(hexadecimal)
        if sn and sn not in encontrados:
            encontrados.append(sn)
    for sn in RE_SN_TEXTO.findall((html or "").upper()):
        if sn not in encontrados:
            encontrados.append(sn)
    return encontrados


async def _entrar(http: httpx.AsyncClient, usuario: str, clave: str) -> None:
    """Inicio de sesión del HG8145: cookie de idioma, token por sesión y clave en base64.

    No se adivina si entró: vale la credencial con la que se puede leer el SN.
    """
    http.cookies.set("Cookie", "body:Language:english:id=-1")
    token = (await http.post("/asp/GetRandCount.asp")).text.strip().lstrip("\ufeff")
    await http.post("/login.cgi", data={
        "UserName": usuario,
        "PassWord": base64.b64encode(clave.encode()).decode(),
        "Language": "english",
        "x.X_HW_Token": token,
    })


async def leer_serial(ip: str, timeout: float = 6.0) -> dict:
    """{"seriales": [...], "error": None} o el motivo por el que no se pudo leer."""
    if not ip:
        return {"seriales": [], "error": "sin IP"}
    try:
        async with httpx.AsyncClient(base_url=f"http://{ip}", timeout=timeout, follow_redirects=False) as http:
            await http.get("/")
            for usuario, clave in credenciales():
                await _entrar(http, usuario, clave)
                for pagina in PAGINAS_DE_INFORMACION:
                    respuesta = await http.get(pagina)
                    seriales = seriales_en_pagina(respuesta.text) if respuesta.status_code == 200 else []
                    if seriales:
                        return {"seriales": seriales, "error": None, "usuario": usuario}
            return {"seriales": [], "error": "no se pudo entrar o la página no muestra el SN"}
    except httpx.TimeoutException:
        return {"seriales": [], "error": "la IP no respondió"}
    except httpx.HTTPError as exc:
        return {"seriales": [], "error": f"sin conexión ({type(exc).__name__})"}
