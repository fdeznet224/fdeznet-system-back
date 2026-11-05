"""El admin del ISP le pregunta al panel por su aviso de pago, firmado y con caché."""
import asyncio

import httpx

from nube.panel import drive
from src.application.services import cuenta_nube_service


def _en_la_nube(monkeypatch):
    monkeypatch.setenv("NUBE_PANEL_URL", "https://panel.ejemplo.com")
    monkeypatch.setenv("NUBE_ISP", "aaa")
    monkeypatch.setenv("NUBE_PANEL_SECRETO", drive.secreto_isp("s" * 64, "aaa"))
    monkeypatch.setattr(cuenta_nube_service, "_cache", {"hasta": 0.0, "estado": None})


def _panel_falso(monkeypatch, responder):
    pedidos = []
    original = httpx.AsyncClient

    def cliente(*args, **kwargs):
        def manejar(peticion):
            pedidos.append(peticion)
            return responder(peticion)
        return original(transport=httpx.MockTransport(manejar))

    monkeypatch.setattr(cuenta_nube_service.httpx, "AsyncClient", cliente)
    return pedidos


def test_fuera_de_la_nube_no_hay_aviso(monkeypatch):
    for variable in ("NUBE_PANEL_URL", "NUBE_ISP", "NUBE_PANEL_SECRETO"):
        monkeypatch.delenv(variable, raising=False)
    assert asyncio.run(cuenta_nube_service.aviso()) is None


def test_pide_el_aviso_firmado_y_lo_guarda_unos_minutos(monkeypatch):
    _en_la_nube(monkeypatch)
    aviso = {"tono": "ambar", "mensaje": "Tu mensualidad de WisPay vence mañana."}
    pedidos = _panel_falso(monkeypatch, lambda p: httpx.Response(200, json={"aviso": aviso}))
    assert asyncio.run(cuenta_nube_service.aviso()) == aviso
    assert asyncio.run(cuenta_nube_service.aviso()) == aviso
    assert len(pedidos) == 1
    parametros = pedidos[0].url.params
    assert pedidos[0].url.path == "/cuenta/estado"
    assert drive.firma_valida("s" * 64, "aaa", "cuenta", parametros["vence"], parametros["firma"])


def test_si_el_panel_no_responde_no_truena(monkeypatch):
    _en_la_nube(monkeypatch)
    _panel_falso(monkeypatch, lambda p: httpx.Response(502))
    assert asyncio.run(cuenta_nube_service.aviso()) is None


def test_pedir_actualizar_va_firmado_y_refresca_la_cuenta(monkeypatch):
    _en_la_nube(monkeypatch)
    pedidos = _panel_falso(monkeypatch, lambda p: httpx.Response(200, json={"ok": True, "version": "2.115.0"}))
    cuenta_nube_service._cache["hasta"] = 9e18
    assert asyncio.run(cuenta_nube_service.actualizar()) == {"ok": True, "version": "2.115.0"}
    assert pedidos[0].method == "POST" and pedidos[0].url.path == "/cuenta/actualizar"
    parametros = pedidos[0].url.params
    assert drive.firma_valida("s" * 64, "aaa", "actualizar", parametros["vence"], parametros["firma"])
    assert cuenta_nube_service._cache["hasta"] == 0.0


def test_actualizar_pasa_el_motivo_del_panel(monkeypatch):
    import pytest
    from fastapi import HTTPException

    _en_la_nube(monkeypatch)
    _panel_falso(monkeypatch, lambda p: httpx.Response(409, json={"detail": "Ya tienes la versión más reciente"}))
    with pytest.raises(HTTPException) as error:
        asyncio.run(cuenta_nube_service.actualizar())
    assert error.value.status_code == 409 and "más reciente" in error.value.detail
