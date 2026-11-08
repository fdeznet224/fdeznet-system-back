"""nube/activar-acs.sh con un docker de mentira: archivos de la instancia e idempotencia."""
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

NUBE = Path(__file__).resolve().parents[1] / "nube"

DOCKER_FALSO = """#!/usr/bin/env bash
echo "$*" >> "$REGISTRO"
# El ping a Mongo responde 1; lo demás no imprime nada.
case "$*" in *runCommand*) echo 1 ;; esac
"""


@pytest.fixture
def nube(tmp_path):
    if not shutil.which("openssl"):
        pytest.skip("sin openssl")
    raiz = tmp_path / "nube"
    raiz.mkdir()
    for archivo in ("activar-acs.sh", "compose.mongo.yml", "compose.isp.yml"):
        shutil.copy(NUBE / archivo, raiz / archivo)
    for slug in ("prueba", "otro"):
        inst = raiz / "instancias" / slug
        inst.mkdir(parents=True)
        (inst / "compose.env").write_text(f"ISP={slug}\nCOMPOSE_PROFILES=whatsapp\n")
        (inst / "api.env").write_text("DB_HOST=mysql\n")
    binarios = tmp_path / "bin"
    binarios.mkdir()
    docker = binarios / "docker"
    docker.write_text(DOCKER_FALSO)
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    registro = tmp_path / "docker.log"
    env = {**os.environ, "PATH": f"{binarios}:{os.environ['PATH']}", "REGISTRO": str(registro)}

    def correr(*args, **extra):
        return subprocess.run(["bash", str(raiz / "activar-acs.sh"), *args], env={**env, **extra},
                              capture_output=True, text=True)

    return raiz, correr, registro


def _env(archivo: Path) -> dict:
    return dict(linea.split("=", 1) for linea in archivo.read_text().splitlines() if "=" in linea)


def test_activa_y_conserva_credenciales(nube):
    raiz, correr, registro = nube
    resultado = correr("prueba", "7548", "prueba.wispay.click")
    assert resultado.returncode == 0, resultado.stderr
    salida = dict(l.split("=", 1) for l in resultado.stdout.splitlines() if l.startswith("ACS_"))
    assert salida["ACS_URL"] == "http://prueba.wispay.click:7548"
    assert salida["ACS_USUARIO"] == "prueba-acs" and len(salida["ACS_CLAVE"]) == 20

    inst = raiz / "instancias" / "prueba"
    compose, api, acs = _env(inst / "compose.env"), _env(inst / "api.env"), _env(inst / "acs.env")
    assert compose["ACS_PORT"] == "7548" and compose["COMPOSE_PROFILES"] == "whatsapp,acs"
    assert api["GENIEACS_NBI_URL"] == "http://acs:7557" and api["ACS_CLAVE"] == salida["ACS_CLAVE"]
    assert acs["GENIEACS_MONGODB_CONNECTION_URL"].startswith("mongodb://acs_prueba:")
    assert (raiz / "secretos" / "mongo_root").stat().st_size > 0
    assert "createUser" in registro.read_text() and "up -d --remove-orphans" in registro.read_text()

    # Otra vez (reparar): mismas credenciales, sin repetir el perfil.
    otra = correr("prueba", "7548", "prueba.wispay.click", ARRANCAR="0")
    assert otra.returncode == 0, otra.stderr
    assert _env(inst / "acs.env") == acs
    assert _env(inst / "compose.env")["COMPOSE_PROFILES"] == "whatsapp,acs"
    assert sum(1 for l in (inst / "api.env").read_text().splitlines() if l.startswith("ACS_URL=")) == 1


def test_puerto_de_otro_isp(nube):
    raiz, correr, _ = nube
    assert correr("prueba", "7548", "prueba.wispay.click").returncode == 0
    resultado = correr("otro", "7548", "otro.wispay.click")
    assert resultado.returncode == 1 and "otro ISP" in resultado.stderr
    assert not (raiz / "instancias" / "otro" / "acs.env").exists()
