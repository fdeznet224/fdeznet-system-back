"""Respaldo de cada ISP en su propio Google Drive.

El ISP conecta su cuenta desde su panel; el permiso es solo drive.file (el
sistema ve únicamente los archivos que él mismo sube). La llave de Google queda
en secretos/drive/<isp>.conf como remoto de rclone, y los respaldos se suben
cifrados con PANEL_RESPALDO_CLAVE: llevan secretos de la instancia.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx

from nube.panel.docker_ops import NUBE_DIR, RESPALDOS, ErrorDocker

CLIENTE_ID = os.getenv("PANEL_GOOGLE_CLIENT_ID", "")
CLIENTE_SECRETO = os.getenv("PANEL_GOOGLE_CLIENT_SECRET", "")
CLAVE_CIFRADO = os.getenv("PANEL_RESPALDO_CLAVE", "")
CARPETA = os.getenv("PANEL_DRIVE_CARPETA", "Respaldos WisPay")
LOCALES_CON_DRIVE = os.getenv("PANEL_RESPALDOS_LOCALES_CON_DRIVE", "1")
DIAS_EN_DRIVE = os.getenv("PANEL_DRIVE_DIAS", "30")

AUTORIZAR = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
REVOCAR = "https://oauth2.googleapis.com/revoke"
PERMISOS = "openid email https://www.googleapis.com/auth/drive.file"
VIGENCIA_ENLACE = 10 * 60
CONFIGS = NUBE_DIR / "secretos" / "drive"


class ErrorDrive(RuntimeError):
    pass


def configurado() -> bool:
    return bool(CLIENTE_ID and CLIENTE_SECRETO and len(CLAVE_CIFRADO) >= 16)


# --- Firmas entre la API del ISP y el panel --------------------------------

def secreto_isp(secreto_panel: str, slug: str) -> str:
    """Secreto que comparte el panel con la API de un ISP (NUBE_PANEL_SECRETO)."""
    return hmac.new(secreto_panel.encode(), f"drive:{slug}".encode(), hashlib.sha256).hexdigest()


def firmar(secreto: str, slug: str, accion: str, vence: int) -> str:
    return hmac.new(secreto.encode(), f"{slug}:{accion}:{vence}".encode(), hashlib.sha256).hexdigest()


def firma_valida(secreto_panel: str, slug: str, accion: str, vence: str, firma: str) -> bool:
    if not vence.isdigit() or not (time.time() <= int(vence) <= time.time() + VIGENCIA_ENLACE):
        return False
    esperada = firmar(secreto_isp(secreto_panel, slug), slug, accion, int(vence))
    return hmac.compare_digest(esperada, firma)


# --- Google ------------------------------------------------------------------

def url_autorizar(regreso: str, estado: str, correo: str = "") -> str:
    parametros = {
        "client_id": CLIENTE_ID,
        "redirect_uri": regreso,
        "response_type": "code",
        "scope": PERMISOS,
        "access_type": "offline",
        # Sin consent Google no vuelve a dar refresh_token a quien ya autorizó.
        "prompt": "consent select_account",
        "state": estado,
    }
    if correo:
        parametros["login_hint"] = correo
    return f"{AUTORIZAR}?{urlencode(parametros)}"


def _correo_de(id_token: str) -> str:
    # Viene directo del endpoint de Google por HTTPS: basta con leerlo.
    try:
        carga = id_token.split(".")[1]
        datos = json.loads(base64.urlsafe_b64decode(carga + "=" * (-len(carga) % 4)))
        return str(datos.get("email", ""))
    except (IndexError, ValueError):
        return ""


def canjear_codigo(codigo: str, regreso: str) -> tuple[dict, str]:
    """Cambia el código de Google por el token de rclone y el correo de la cuenta."""
    respuesta = httpx.post(TOKEN, timeout=20, data={
        "code": codigo, "client_id": CLIENTE_ID, "client_secret": CLIENTE_SECRETO,
        "redirect_uri": regreso, "grant_type": "authorization_code",
    })
    datos = respuesta.json() if respuesta.headers.get("content-type", "").startswith("application/json") else {}
    if respuesta.status_code != 200 or not datos.get("refresh_token"):
        raise ErrorDrive(datos.get("error_description") or datos.get("error") or "Google no dio acceso")
    if "drive.file" not in datos.get("scope", ""):
        raise ErrorDrive("No se dio permiso para guardar en Drive; vuelve a conectar y marca la casilla de Drive")
    vence = datetime.now(timezone.utc) + timedelta(seconds=int(datos.get("expires_in", 3600)))
    token = {
        "access_token": datos["access_token"],
        "token_type": "Bearer",
        "refresh_token": datos["refresh_token"],
        "expiry": vence.isoformat(timespec="seconds"),
    }
    return token, _correo_de(datos.get("id_token", ""))


# --- Remoto de rclone por ISP --------------------------------------------------

def _conf(slug: str) -> Path:
    return CONFIGS / f"{slug}.conf"


def conectado(slug: str) -> bool:
    return _conf(slug).is_file()


def _ocultar(clave: str) -> str:
    resultado = subprocess.run(["rclone", "obscure", "-"], input=clave, capture_output=True, text=True, timeout=30)
    if resultado.returncode != 0:
        raise ErrorDrive("rclone no pudo preparar la clave de cifrado")
    return resultado.stdout.strip()


def guardar_conexion(slug: str, token: dict) -> None:
    sal = hmac.new(CLAVE_CIFRADO.encode(), slug.encode(), hashlib.sha256).hexdigest()
    contenido = (
        "[drive]\n"
        "type = drive\n"
        f"client_id = {CLIENTE_ID}\n"
        f"client_secret = {CLIENTE_SECRETO}\n"
        "scope = drive.file\n"
        f"token = {json.dumps(token)}\n"
        "\n"
        "[cifrado]\n"
        "type = crypt\n"
        f"remote = drive:{CARPETA}\n"
        "filename_encryption = off\n"
        "directory_name_encryption = false\n"
        f"password = {_ocultar(CLAVE_CIFRADO)}\n"
        f"password2 = {_ocultar(sal)}\n"
    )
    CONFIGS.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporal = _conf(slug).with_suffix(".parcial")
    temporal.write_text(contenido, encoding="utf-8")
    temporal.chmod(0o600)
    temporal.replace(_conf(slug))


def desconectar(slug: str) -> None:
    """Le quita el permiso al sistema en Google y borra la llave; lo ya subido se queda en su Drive."""
    ruta = _conf(slug)
    if not ruta.is_file():
        return
    coincide = re.search(r"^token = (.+)$", ruta.read_text(encoding="utf-8"), re.MULTILINE)
    if coincide:
        try:
            refresco = json.loads(coincide.group(1)).get("refresh_token", "")
            httpx.post(REVOCAR, data={"token": refresco}, timeout=15)
        except (ValueError, httpx.HTTPError):
            pass  # Si Google no responde, igual se olvida la llave aquí.
    ruta.unlink()


def ajustes_respaldo(slug: str) -> dict[str, str]:
    """Variables para respaldar-instancia.sh cuando el ISP tiene Drive conectado."""
    return {
        "RCLONE_CONFIG": str(_conf(slug)),
        "RESPALDO_REMOTO": "cifrado:",
        "RESPALDO_DIAS_REMOTO": DIAS_EN_DRIVE,
        "RESPALDOS_LOCALES": LOCALES_CON_DRIVE,
    }


def _rclone(slug: str, *args: str, timeout: int = 120) -> str:
    resultado = subprocess.run(
        ["rclone", *args], capture_output=True, text=True, timeout=timeout,
        env={**os.environ, "RCLONE_CONFIG": str(_conf(slug))},
    )
    if resultado.returncode != 0:
        ultima = (resultado.stderr or "").strip().splitlines()[-1:] or ["rclone falló"]
        raise ErrorDocker(f"Drive: {ultima[0][-300:]}")
    return resultado.stdout


def listar(slug: str) -> list[dict]:
    """Respaldos del ISP en su Drive, del más reciente al más viejo."""
    if not conectado(slug):
        return []
    archivos = json.loads(_rclone(slug, "lsjson", "--files-only", f"cifrado:{slug}") or "[]")
    patron = rf"{re.escape(slug)}-(\d{{8}}-\d{{6}})\.tar"
    lista = []
    for archivo in archivos:
        coincide = re.fullmatch(patron, archivo.get("Name", ""))
        if coincide:
            lista.append({
                "nombre": archivo["Name"], "tamano": archivo.get("Size", 0),
                "fecha": datetime.strptime(coincide.group(1), "%Y%m%d-%H%M%S"),
            })
    return sorted(lista, key=lambda a: a["nombre"], reverse=True)


def traer(slug: str, nombre: str) -> None:
    """Baja un respaldo del Drive del ISP a la VPS para poder restaurarlo."""
    if not re.fullmatch(rf"{re.escape(slug)}-\d{{8}}-\d{{6}}\.tar", nombre):
        raise ErrorDocker("Respaldo inválido")
    destino = RESPALDOS / slug
    destino.mkdir(mode=0o700, parents=True, exist_ok=True)
    _rclone(slug, "copy", f"cifrado:{slug}/{nombre}", str(destino), timeout=1800)
