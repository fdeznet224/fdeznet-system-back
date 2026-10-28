"""Control de los contenedores de cada ISP con docker compose.

El panel corre con el directorio nube/ montado en la misma ruta que en la VPS,
así las rutas relativas de los compose apuntan a lo mismo dentro y fuera.
"""
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

from nube.panel.cuentas import poner_variable

NUBE_DIR = Path(os.getenv("NUBE_DIR", Path(__file__).resolve().parents[1]))
COMPOSE_ISP = NUBE_DIR / "compose.isp.yml"


class ErrorDocker(RuntimeError):
    pass


def _correr(comando: list[str], timeout: int = 300, env: dict | None = None) -> str:
    resultado = subprocess.run(
        comando,
        cwd=NUBE_DIR,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, **(env or {})},
    )
    if resultado.returncode != 0:
        salida = (resultado.stderr or resultado.stdout).strip().splitlines()
        raise ErrorDocker("\n".join(salida[-8:]) or f"Falló: {' '.join(comando[:3])}")
    return resultado.stdout


def _instancia(slug: str) -> Path:
    return NUBE_DIR / "instancias" / slug


def _compose(slug: str, *args: str, timeout: int = 300) -> str:
    return _correr(
        [
            "docker", "compose", "-p", slug,
            "--env-file", str(_instancia(slug) / "compose.env"),
            "-f", str(COMPOSE_ISP), *args,
        ],
        timeout=timeout,
    )


def crear(slug: str, web_port: int, wg_port: int, dominio: str, *, limite_clientes: int | None,
          limite_routers: int | None, whatsapp: bool, version: str) -> str:
    """Da de alta la instancia y devuelve la contraseña inicial del admin."""
    salida = _correr(
        [str(NUBE_DIR / "crear-instancia.sh"), slug, str(web_port), str(wg_port), dominio],
        timeout=900,
        env={
            "LIMITE_CLIENTES": "" if limite_clientes is None else str(limite_clientes),
            "LIMITE_ROUTERS": "" if limite_routers is None else str(limite_routers),
            "CON_WHATSAPP": "1" if whatsapp else "0",
            "IMAGEN_VERSION": version,
        },
    )
    coincide = re.search(r"^ADMIN_PASSWORD=(\S+)$", salida, re.MULTILINE)
    if not coincide:
        raise ErrorDocker("La instancia se creó pero no se obtuvo la contraseña del admin")
    return coincide.group(1)


def puertos_en_uso() -> tuple[list[int], list[int]]:
    """Puertos web y WireGuard de todas las instancias en disco, aunque no estén en el panel."""
    web, wg = [], []
    for archivo in (NUBE_DIR / "instancias").glob("*/compose.env"):
        for linea in archivo.read_text(encoding="utf-8").splitlines():
            clave, _, valor = linea.partition("=")
            if valor.strip().isdigit():
                if clave == "WEB_PORT":
                    web.append(int(valor))
                elif clave == "WG_PORT":
                    wg.append(int(valor))
    return web, wg


def _editar_env(archivo: Path, valores: dict[str, str]) -> None:
    contenido = archivo.read_text(encoding="utf-8")
    for clave, valor in valores.items():
        contenido = poner_variable(contenido, clave, valor)
    archivo.write_text(contenido, encoding="utf-8")


def aplicar_ajustes(slug: str, *, version: str, whatsapp: bool, limite_clientes: int | None,
                    limite_routers: int | None, encendido: bool) -> None:
    """Guarda versión, bot y límites; si el ISP está activo, recrea lo que cambió."""
    _editar_env(_instancia(slug) / "compose.env", {
        "IMAGEN_VERSION": version,
        "COMPOSE_PROFILES": "whatsapp" if whatsapp else "",
    })
    _editar_env(_instancia(slug) / "api.env", {
        "FDEZNET_LIMITE_CLIENTES": "" if limite_clientes is None else str(limite_clientes),
        "FDEZNET_LIMITE_ROUTERS": "" if limite_routers is None else str(limite_routers),
    })
    if not encendido:
        return
    if not whatsapp:
        try:
            _correr(["docker", "rm", "-f", f"{slug}-bot-1"], timeout=60)
        except ErrorDocker:
            pass  # No tenía bot.
    _compose(slug, "up", "-d", "--remove-orphans", timeout=600)


def detener(slug: str) -> None:
    _compose(slug, "--profile", "whatsapp", "stop", timeout=120)


def encender(slug: str) -> None:
    _compose(slug, "up", "-d", timeout=600)


def estado_contenedores() -> dict[str, dict[str, str]]:
    """{slug: {servicio: estado}} de todos los ISP, con una sola llamada."""
    salida = _correr(["docker", "ps", "-a", "--format", "{{json .}}"], timeout=30)
    estados: dict[str, dict[str, str]] = {}
    for linea in salida.splitlines():
        datos = json.loads(linea)
        coincide = re.fullmatch(r"(.+)-(api|bot|web)-1", datos.get("Names", ""))
        if coincide:
            estados.setdefault(coincide.group(1), {})[coincide.group(2)] = datos.get("Status", "")
    return estados


def uso_memoria() -> dict[str, float]:
    """MB de RAM que usa cada contenedor que está corriendo."""
    salida = _correr(["docker", "stats", "--no-stream", "--format", "{{json .}}"], timeout=60)
    uso: dict[str, float] = {}
    unidades = {"B": 1 / 1048576, "KiB": 1 / 1024, "MiB": 1, "GiB": 1024}
    for linea in salida.splitlines():
        datos = json.loads(linea)
        coincide = re.match(r"([\d.]+)(B|KiB|MiB|GiB)", datos.get("MemUsage", ""))
        if coincide:
            uso[datos["Name"]] = float(coincide.group(1)) * unidades[coincide.group(2)]
    return uso


def versiones_disponibles() -> list[str]:
    """Versiones de la API ya construidas en la VPS (nube/construir-version.sh)."""
    salida = _correr(["docker", "images", "fdezpay-api", "--format", "{{.Tag}}"], timeout=30)
    etiquetas = {etiqueta for etiqueta in salida.split() if etiqueta != "<none>"}

    def orden(etiqueta: str):
        partes = etiqueta.split(".")
        return [int(p) for p in partes] if all(p.isdigit() for p in partes) else [-1]

    return sorted(etiquetas, key=orden, reverse=True)


# --- Respaldos ------------------------------------------------------------

RESPALDOS = NUBE_DIR / "respaldos"


def _ajustes_respaldo() -> dict[str, str]:
    return {
        "RESPALDOS_LOCALES": os.getenv("PANEL_RESPALDOS_LOCALES", "7"),
        "RESPALDO_REMOTO": os.getenv("PANEL_RESPALDO_REMOTO", ""),
        "RESPALDO_DIAS_REMOTO": os.getenv("PANEL_RESPALDO_DIAS_REMOTO", "30"),
    }


def _claves(salida: str) -> dict[str, str]:
    return dict(re.findall(r"^([A-Z]+)=(.*)$", salida, re.MULTILINE))


def respaldar(slug: str) -> dict[str, str]:
    """Respalda base, volúmenes y secretos; devuelve ARCHIVO, TAMANO y REMOTO."""
    salida = _correr([str(NUBE_DIR / "respaldar-instancia.sh"), slug], timeout=1800, env=_ajustes_respaldo())
    datos = _claves(salida)
    if "ARCHIVO" not in datos:
        raise ErrorDocker("El respaldo terminó sin archivo")
    return datos


def archivo_respaldo(slug: str, nombre: str) -> Path:
    """Ruta de un respaldo del ISP; el nombre no puede salirse de su carpeta."""
    if not re.fullmatch(rf"{re.escape(slug)}-\d{{8}}-\d{{6}}\.tar", nombre):
        raise ErrorDocker("Respaldo inválido")
    ruta = RESPALDOS / slug / nombre
    if not ruta.is_file():
        raise ErrorDocker("Ese respaldo ya no está en la VPS")
    return ruta


def listar_respaldos(slug: str) -> list[dict]:
    """Respaldos guardados en la VPS, del más reciente al más viejo."""
    archivos = [
        archivo for archivo in (RESPALDOS / slug).glob(f"{slug}-*.tar")
        if re.fullmatch(rf"{re.escape(slug)}-\d{{8}}-\d{{6}}\.tar", archivo.name)
    ]
    return [
        {"nombre": archivo.name, "tamano": archivo.stat().st_size,
         "fecha": datetime.strptime(archivo.name[len(slug) + 1:-4], "%Y%m%d-%H%M%S")}
        for archivo in sorted(archivos, key=lambda a: a.name, reverse=True)
    ]


def restaurar(slug: str, nombre: str, *, arrancar: bool) -> None:
    ruta = archivo_respaldo(slug, nombre)
    _correr(
        [str(NUBE_DIR / "restaurar-instancia.sh"), slug, str(ruta)],
        timeout=1800,
        env={**_ajustes_respaldo(), "ARRANCAR": "1" if arrancar else "0"},
    )


def restablecer_admin(slug: str, usuario: str, clave: str) -> None:
    """Contraseña nueva para el admin del ISP; va por variable, no queda en el historial."""
    _correr(
        ["docker", "exec", "-e", "RESTABLECER_USUARIO", "-e", "RESTABLECER_PASSWORD",
         f"{slug}-api-1", "python", "-m", "src.restablecer_admin"],
        timeout=120,
        env={"RESTABLECER_USUARIO": usuario, "RESTABLECER_PASSWORD": clave},
    )
