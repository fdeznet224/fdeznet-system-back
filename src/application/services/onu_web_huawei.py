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
RE_SSL_PORT = re.compile(r"var SSLPort\s*=\s*'(\d+)'")


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


async def _direccion_web(ip: str, timeout: float) -> str:
    """El HG8145V5 contesta en HTTP solo para mandar a su página HTTPS (SSLPort)."""
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as http:
        inicio = await http.get(f"http://{ip}/")
    puerto = RE_SSL_PORT.search(inicio.text or "")
    return f"https://{ip}:{puerto.group(1)}" if puerto else f"http://{ip}"


# ------------------------------------------------------------------ filtro MAC
# El HG8145V5 no publica su filtro MAC por TR-069: se maneja con los mismos
# formularios de su web (Seguridad → Filtro MAC). Política 0 = lista negra;
# la 1 (lista blanca) dejaría sin internet a todo lo que no esté en la lista.
PAGINA_FILTRO_MAC = "html/bbsp/macfilter/macfilter.asp"
PAGINA_CON_TOKEN = "/html/ssmp/tr069/tr069.asp"
DOMINIO_SEGURIDAD = "InternetGatewayDevice.X_HW_Security"
RE_FILTRO = re.compile(r'new stMacFilter\("([^"]+)","([^"]+)"\)')
RE_TOKEN = re.compile(r'id="onttoken"[^>]*value="([^"]+)"')


def normalizar_mac(mac: str) -> str:
    """"AA-BB-CC-DD-EE-FF" o "aabb.ccdd.eeff" → "aa:bb:cc:dd:ee:ff"."""
    limpia = re.sub(r"[^0-9a-fA-F]", "", mac or "").lower()
    if len(limpia) != 12:
        raise ValueError("MAC inválida.")
    return ":".join(limpia[i:i + 2] for i in range(0, 12, 2))


def _sin_escapes(texto: str) -> str:
    """La web escribe "08\\x3a65" en lugar de "08:65"."""
    return re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), texto)


def filtro_en_pagina(html: str) -> dict:
    """{"activo": bool, "lista_negra": bool, "macs": {mac: dominio}} de la página del filtro."""
    activo = re.search(r"var enableFilter = '(\d)'", html or "")
    modo = re.search(r"var Mode = '(\d)'", html or "")
    if not activo or not modo:
        raise ValueError("La ONU no mostró su filtro MAC (¿usuario sin permiso?).")
    macs = {}
    for dominio, mac in RE_FILTRO.findall(html):
        try:
            macs[normalizar_mac(_sin_escapes(mac))] = dominio
        except ValueError:
            continue
    return {"activo": activo.group(1) == "1", "lista_negra": modo.group(1) == "0", "macs": macs}


class FiltroMacHuawei:
    """Una sesión en la web de la ONU; siempre se cierra al terminar porque
    el HG8145 deja entrar a un solo administrador a la vez."""

    def __init__(self, ip: str, cuentas: list[tuple[str, str]], timeout: float = 10.0):
        self.ip, self.cuentas, self.timeout = ip, cuentas or credenciales(), timeout
        self.http: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "FiltroMacHuawei":
        base = await _direccion_web(self.ip, self.timeout)
        # Certificado autofirmado de la ONU (ver leer_serial).
        self.http = httpx.AsyncClient(base_url=base, timeout=self.timeout, follow_redirects=False, verify=False)
        await self.http.get("/")
        for usuario, clave in self.cuentas:
            await _entrar(self.http, usuario, clave)
            try:
                self.estado_inicial = filtro_en_pagina((await self.http.get("/" + PAGINA_FILTRO_MAC)).text)
                return self
            except ValueError:
                continue
        await self.http.aclose()
        raise ValueError("No se pudo entrar a la web de la ONU con las cuentas configuradas.")

    async def __aexit__(self, *_):
        try:
            await self.http.post("/logout.cgi?RequestFile=html/logout.html")
        except httpx.HTTPError:
            pass
        await self.http.aclose()

    async def _token(self) -> str:
        encontrado = RE_TOKEN.search((await self.http.get(PAGINA_CON_TOKEN)).text)
        if not encontrado:
            raise ValueError("La ONU no entregó el token de la sesión.")
        return encontrado.group(1)

    async def _enviar(self, cgi: str, dominio: str, datos: dict) -> None:
        await self.http.post(f"/{cgi}?x={dominio}&RequestFile={PAGINA_FILTRO_MAC}",
                             data={**datos, "x.X_HW_Token": await self._token()})

    async def estado(self) -> dict:
        return filtro_en_pagina((await self.http.get("/" + PAGINA_FILTRO_MAC)).text)

    async def bloquear(self, mac: str) -> dict:
        mac = normalizar_mac(mac)
        estado = await self.estado()
        if not estado["activo"] or not estado["lista_negra"]:
            # Política y encendido juntos: nunca queda activa una lista blanca.
            await self._enviar("set.cgi", DOMINIO_SEGURIDAD, {"x.MacFilterPolicy": "0", "x.MacFilterRight": "1"})
        if mac not in estado["macs"]:
            await self._enviar("add.cgi", f"{DOMINIO_SEGURIDAD}.MacFilter", {"x.SourceMACAddress": mac})
        estado = await self.estado()
        if mac not in estado["macs"] or not estado["activo"] or not estado["lista_negra"]:
            raise ValueError("La ONU no guardó el bloqueo.")
        return estado

    async def desbloquear(self, mac: str) -> dict:
        mac = normalizar_mac(mac)
        estado = await self.estado()
        if mac in estado["macs"]:
            await self._enviar("del.cgi", f"{DOMINIO_SEGURIDAD}.MacFilter", {estado["macs"][mac]: ""})
            estado = await self.estado()
        if mac in estado["macs"]:
            raise ValueError("La ONU no quitó el bloqueo.")
        if not estado["macs"] and estado["activo"]:
            await self._enviar("set.cgi", DOMINIO_SEGURIDAD, {"x.MacFilterRight": "0"})
            estado = await self.estado()
        return estado


async def leer_serial(ip: str, timeout: float = 6.0) -> dict:
    """{"seriales": [...], "error": None} o el motivo por el que no se pudo leer."""
    if not ip:
        return {"seriales": [], "error": "sin IP"}
    try:
        base = await _direccion_web(ip, timeout)
        # La ONU firma su propio certificado: no hay CA que lo valide. Se acepta
        # solo aquí, para leer el SN de equipos propios en la red del ISP.
        async with httpx.AsyncClient(base_url=base, timeout=timeout, follow_redirects=False, verify=False) as http:
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
