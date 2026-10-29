"""La API del ISP firma lo que le pide al panel igual que el panel lo revisa."""
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import HTTPException

from nube.panel import drive
from src.application.services import respaldo_drive_service


def test_enlace_conectar_lo_acepta_el_panel(monkeypatch):
    monkeypatch.setenv("NUBE_PANEL_URL", "https://panel.ejemplo.com/")
    monkeypatch.setenv("NUBE_ISP", "aaa")
    monkeypatch.setenv("NUBE_PANEL_SECRETO", drive.secreto_isp("s" * 64, "aaa"))
    enlace = urlparse(respaldo_drive_service.enlace_conectar("red@gmail.com"))
    assert f"{enlace.scheme}://{enlace.netloc}{enlace.path}" == "https://panel.ejemplo.com/drive/conectar"
    datos = {clave: valor[0] for clave, valor in parse_qs(enlace.query).items()}
    assert datos["correo"] == "red@gmail.com"
    assert drive.firma_valida("s" * 64, "aaa", "conectar", datos["vence"], datos["firma"])
    assert not drive.firma_valida("s" * 64, "aaa", "desconectar", datos["vence"], datos["firma"])


def test_fuera_de_la_nube_no_esta_disponible(monkeypatch):
    for variable in ("NUBE_PANEL_URL", "NUBE_ISP", "NUBE_PANEL_SECRETO"):
        monkeypatch.delenv(variable, raising=False)
    with pytest.raises(HTTPException) as error:
        respaldo_drive_service.enlace_conectar()
    assert error.value.status_code == 404
