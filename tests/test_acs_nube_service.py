"""ACS propio del ISP en la nube: datos para el módem y la petición firmada al panel."""
import asyncio

import httpx
import pytest
from fastapi import HTTPException

from nube.panel import drive
from src.application.services import acs_nube_service


def _en_la_nube(monkeypatch):
    monkeypatch.setenv("NUBE_PANEL_URL", "https://panel.ejemplo.com")
    monkeypatch.setenv("NUBE_ISP", "aaa")
    monkeypatch.setenv("NUBE_PANEL_SECRETO", drive.secreto_isp("s" * 64, "aaa"))
    monkeypatch.delenv("ACS_URL", raising=False)


def _panel_falso(monkeypatch, responder):
    pedidos = []
    original = httpx.AsyncClient

    def cliente(*args, **kwargs):
        def manejar(peticion):
            pedidos.append(peticion)
            return responder(peticion)
        return original(transport=httpx.MockTransport(manejar))

    monkeypatch.setattr(acs_nube_service.httpx, "AsyncClient", cliente)
    return pedidos


def test_datos_del_acs_activo(monkeypatch):
    _en_la_nube(monkeypatch)
    assert acs_nube_service.datos()["activo"] is False
    monkeypatch.setenv("ACS_URL", "http://aaa.wispay.click:7548")
    monkeypatch.setenv("ACS_USUARIO", "aaa-acs")
    monkeypatch.setenv("ACS_CLAVE", "Clave123")
    assert acs_nube_service.datos() == {"en_la_nube": True, "activo": True, "url": "http://aaa.wispay.click:7548",
                                        "usuario": "aaa-acs", "clave": "Clave123", "intervalo": 300}


def test_activar_va_firmado_al_panel(monkeypatch):
    _en_la_nube(monkeypatch)
    pedidos = _panel_falso(monkeypatch, lambda p: httpx.Response(200, json={"ok": True}))
    assert asyncio.run(acs_nube_service.activar()) == {"ok": True}
    assert pedidos[0].method == "POST" and pedidos[0].url.path == "/cuenta/acs"
    parametros = pedidos[0].url.params
    assert drive.firma_valida("s" * 64, "aaa", "acs", parametros["vence"], parametros["firma"])


def test_activar_pasa_el_motivo_del_panel(monkeypatch):
    _en_la_nube(monkeypatch)
    _panel_falso(monkeypatch, lambda p: httpx.Response(409, json={"detail": "El sistema está suspendido"}))
    with pytest.raises(HTTPException) as error:
        asyncio.run(acs_nube_service.activar())
    assert error.value.status_code == 409 and "suspendido" in error.value.detail


def test_fuera_de_la_nube_no_se_activa(monkeypatch):
    for variable in ("NUBE_PANEL_URL", "NUBE_ISP", "NUBE_PANEL_SECRETO"):
        monkeypatch.delenv(variable, raising=False)
    with pytest.raises(HTTPException) as error:
        asyncio.run(acs_nube_service.activar())
    assert error.value.status_code == 404
