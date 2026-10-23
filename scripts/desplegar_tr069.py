"""Activa TR-069 en ONU Huawei de clientes por su web (como se hizo con la del dueño).

Uso (en el VPS, como root):
  python desplegar_tr069.py candidatos.json registro.jsonl [--limite N] [--solo-leer]

candidatos.json: [{"servicio_id", "cliente_id", "ip", "serial"}]
Por cada ONU: entra con telecomadmin, guarda cómo estaba (TR-069 y WAN) en el
registro, pone el ACS y cambia la WAN PPPoE de internet a TR069_INTERNET
(reconecta el PPPoE unos segundos). Luego espera a que se reporte en GenieACS.
"""

import asyncio
import json
import re
import sys
import time
from datetime import datetime

import httpx

sys.path.insert(0, "/opt/fdeznet/backend")
from src.application.services.onu_web_huawei import _direccion_web, _entrar, _sin_escapes  # noqa: E402

ACS = "http://195.26.241.105:7547/"
NBI = "http://127.0.0.1:7557"
CUENTA = ("telecomadmin", "admintelecom")
RE_TOKEN = re.compile(r'<input[^>]*(?:id|name)="(?:hw)?onttoken"[^>]*value="([0-9a-fA-F]+)"')


def env_acs() -> dict:
    datos = {}
    for linea in open("/opt/genieacs/.env"):
        if "=" in linea:
            k, v = linea.strip().split("=", 1)
            datos[k] = v
    return datos


def campos(texto: str) -> list:
    return [_sin_escapes(c) for c in re.findall(r'"((?:[^"\\]|\\.)*)"', texto)]


def leer_cwmp(html: str) -> dict | None:
    m = re.search(r"new stCWMP\((.*?)\),null\);", html)
    if not m:
        return None
    f = campos(m.group(1))
    return {"enable": f[1], "inform": f[2], "intervalo": f[3], "url": f[5], "usuario": f[6]}


RE_NOMBRE_WAN = re.compile(r"^\d+_[A-Z0-9_]+_[RB](_VID_\d+)?$")
MODOS_WAN = ("IP_Routed", "IP_Bridged", "PPPoE_Bridged")


def leer_wans(html: str) -> list:
    """Por contenido, no por posición: hay firmwares con un campo de más al inicio."""
    wans = []
    for tipo, cuerpo in re.findall(r"new (WanPPP|WanIP)\((.*?)\)[,;]", html):
        f = campos(cuerpo)
        if len(f) < 12:
            continue
        nombre = next((c for c in f if RE_NOMBRE_WAN.match(c)), "")
        modo = next((c for c in f if c in MODOS_WAN), "")
        wans.append({"tipo": tipo, "dominio": f[0], "nombre": nombre, "modo": modo})
    return wans


async def token(http, pagina: str) -> str:
    encontrado = RE_TOKEN.search((await http.get(pagina)).text)
    if not encontrado:
        raise RuntimeError("sin token")
    return encontrado.group(1)


async def configurar(onu: dict, acs: dict, solo_leer: bool) -> dict:
    resultado = {**onu, "hora": datetime.now().isoformat(timespec="seconds")}
    try:
        base = await _direccion_web(onu["ip"], 8)
    except httpx.HTTPError as error:
        return {**resultado, "resultado": "sin_respuesta", "detalle": type(error).__name__}
    async with httpx.AsyncClient(base_url=base, timeout=15, verify=False, follow_redirects=False) as http:
        try:
            await http.get("/")
            await _entrar(http, *CUENTA)
            tr = (await http.get("/html/ssmp/tr069/tr069.asp")).text
            cwmp = leer_cwmp(tr)
            if not cwmp:
                return {**resultado, "resultado": "sin_acceso", "detalle": "telecomadmin no entró o firmware distinto"}
            wans = leer_wans((await http.get("/html/bbsp/common/wan_list.asp")).text)
            if not wans:
                # Firmware nuevo (HG8145X6 V5R022): la lista se pide aparte.
                wans = leer_wans((await http.get("/html/bbsp/common/wan_list_cache_wan.asp")).text)
            resultado["antes"] = {"cwmp": cwmp, "wans": wans}
            internet = [w for w in wans if w["tipo"] == "WanPPP" and w["modo"] == "IP_Routed" and "INTERNET" in w["nombre"]]
            ya_tr069 = any("TR069" in w["nombre"] for w in wans)
            if solo_leer:
                return {**resultado, "resultado": "leido", "wan_internet": [w["nombre"] for w in internet], "ya_tr069": ya_tr069}
            if not ya_tr069 and len(internet) != 1:
                return {**resultado, "resultado": "omitida", "detalle": f"{len(internet)} WAN PPPoE de internet"}
            if ya_tr069 and cwmp["url"] == ACS and cwmp["enable"] == "1":
                return {**resultado, "resultado": "ya_estaba"}
            if cwmp["url"] != ACS or cwmp["enable"] != "1" or cwmp["intervalo"] != "300":
                await http.post("/set.cgi?x=InternetGatewayDevice.ManagementServer&RequestFile=html/ssmp/tr069/tr069.asp", data={
                    "x.EnableCWMP": "1", "x.URL": ACS, "x.Username": acs["ACS_USUARIO"], "x.Password": acs["ACS_CLAVE"],
                    "x.PeriodicInformEnable": "1", "x.PeriodicInformInterval": "300",
                    "x.ConnectionRequestUsername": acs["CR_USUARIO"], "x.ConnectionRequestPassword": acs["CR_CLAVE"],
                    "x.X_HW_Token": await token(http, "/html/ssmp/tr069/tr069.asp"),
                })
                if (leer_cwmp((await http.get("/html/ssmp/tr069/tr069.asp")).text) or {}).get("url") != ACS:
                    return {**resultado, "resultado": "error", "detalle": "la ONU no guardó el ACS"}
            if not ya_tr069:
                wan = internet[0]
                # La WAN se reconecta: la respuesta puede no llegar.
                try:
                    await http.post(f"/complex.cgi?y={wan['dominio']}&RequestFile=html/bbsp/wan/confirmwancfginfo.html",
                                    data={"y.X_HW_SERVICELIST": "TR069_INTERNET", "x.X_HW_Token": await token(http, "/html/ssmp/tr069/tr069.asp")})
                except httpx.HTTPError:
                    pass
                resultado["wan_cambiada"] = wan["nombre"]
        finally:
            try:
                await http.post("/logout.cgi?RequestFile=html/logout.html")
            except httpx.HTTPError:
                pass
    return {**resultado, "resultado": "configurada"}


async def reportada(serial_hex: str, ip: str, desde: float, espera: int = 150) -> bool:
    consulta = {"$or": [{"_deviceId._SerialNumber": serial_hex},
                        {"InternetGatewayDevice.ManagementServer.ConnectionRequestURL._value": {"$regex": f"^http://{re.escape(ip)}:"}}]}
    limite = time.time() + espera
    async with httpx.AsyncClient(base_url=NBI, timeout=10) as nbi:
        while time.time() < limite:
            filas = (await nbi.get("/devices", params={"query": json.dumps(consulta), "projection": "_id,_lastInform"})).json()
            for fila in filas:
                if datetime.fromisoformat(fila["_lastInform"].replace("Z", "+00:00")).timestamp() >= desde - 5:
                    return True
            await asyncio.sleep(10)
    return False


def serial_hex(serial: str) -> str:
    s = re.sub(r"[^0-9A-Za-z]", "", serial or "").upper()
    return s[:4].encode().hex().upper() + s[4:] if re.fullmatch(r"[A-Z]{4}[0-9A-F]{8}", s) else s


async def main():
    candidatos = json.load(open(sys.argv[1]))
    registro = sys.argv[2]
    limite = int(sys.argv[sys.argv.index("--limite") + 1]) if "--limite" in sys.argv else len(candidatos)
    solo_leer = "--solo-leer" in sys.argv
    acs = env_acs()
    # Si se cortó (p. ej. se cayó el SSH), retoma: salta las que ya quedaron en el registro.
    terminadas = set()
    try:
        for linea in open(registro):
            r = json.loads(linea)
            if r.get("resultado") in ("configurada", "ya_estaba", "leido"):
                terminadas.add(r.get("servicio_id"))
    except FileNotFoundError:
        pass
    hechas = 0
    for onu in candidatos:
        if hechas >= limite:
            break
        if onu.get("servicio_id") in terminadas:
            continue
        inicio = time.time()
        r = await configurar(onu, acs, solo_leer)
        if r["resultado"] == "configurada":
            r["reportada"] = await reportada(serial_hex(onu["serial"]), onu["ip"], inicio)
        with open(registro, "a") as salida:
            salida.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(onu["cliente_id"], r["resultado"], r.get("detalle", ""), r.get("wan_cambiada", ""), "reportada" if r.get("reportada") else "", flush=True)
        if r["resultado"] in ("configurada", "leido", "omitida", "error", "ya_estaba"):
            hechas += 1


asyncio.run(main())
