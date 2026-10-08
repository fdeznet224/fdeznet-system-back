import asyncio

import httpx

import src.application.services.onu_web_huawei as huawei


def test_el_sn_se_lee_en_hexadecimal_o_en_texto():
    pagina = 'new stDeviceInfo("InternetGatewayDevice.DeviceInfo","HG8145V5","485754431A2B3C4D","V5R020C00S115")'
    assert huawei.seriales_en_pagina(pagina) == ["HWTC1A2B3C4D"]
    assert huawei.seriales_en_pagina("SN: HWTC05450CB6 (48575443 05450CB6)") == ["HWTC05450CB6"]
    assert huawei.sn_de_hex("0000000012345678") is None   # no es un fabricante


def test_credenciales_configurables(monkeypatch):
    monkeypatch.delenv("ONU_WEB_CREDENCIALES", raising=False)
    assert huawei.credenciales() == [("telecomadmin", "admintelecom"), ("root", "admin")]
    monkeypatch.setenv("ONU_WEB_CREDENCIALES", "admin:x1,soporte:y2")
    assert huawei.credenciales() == [("admin", "x1"), ("soporte", "y2")]


def test_prueba_las_credenciales_hasta_leer_el_sn(monkeypatch):
    monkeypatch.delenv("ONU_WEB_CREDENCIALES", raising=False)
    entradas = []

    def onu(request: httpx.Request):
        if request.url.path == "/login.cgi":
            datos = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            entradas.append(datos["UserName"])
            return httpx.Response(200, headers={"set-cookie": f"Cookie=sid={datos['UserName']}"})
        if request.url.path == "/asp/GetRandCount.asp":
            return httpx.Response(200, text="﻿token123")
        if request.url.path.endswith("deviceinfo.asp"):
            # Solo con la segunda credencial (root) deja ver la información.
            if "sid=root" in request.headers.get("cookie", ""):
                return httpx.Response(200, text='"485754431A2B3C4D"')
            return httpx.Response(200, text="<html>login</html>")
        return httpx.Response(200, text="<html></html>")

    transporte = httpx.MockTransport(onu)
    original = httpx.AsyncClient
    monkeypatch.setattr(huawei.httpx, "AsyncClient", lambda **kw: original(transport=transporte, **kw))
    resultado = asyncio.run(huawei.leer_serial("10.20.0.5"))
    assert resultado == {"seriales": ["HWTC1A2B3C4D"], "error": None, "usuario": "root"}
    assert entradas == ["telecomadmin", "root"]


def test_sigue_a_la_pagina_https_del_hg8145v5(monkeypatch):
    monkeypatch.delenv("ONU_WEB_CREDENCIALES", raising=False)
    visitadas = []

    def onu(request: httpx.Request):
        visitadas.append(f"{request.url.scheme}:{request.url.port}{request.url.path}")
        if request.url.scheme == "http":
            return httpx.Response(200, text="<script>var SSLPort ='80'; location.href='https://'+host;</script>")
        if request.url.path.endswith("deviceinfo.asp"):
            return httpx.Response(200, text='"485754431A2B3C4D"')
        return httpx.Response(200, text="token")

    original = httpx.AsyncClient
    monkeypatch.setattr(huawei.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(onu), **kw))
    resultado = asyncio.run(huawei.leer_serial("10.10.9.2"))
    assert resultado["seriales"] == ["HWTC1A2B3C4D"]
    assert visitadas[0] == "http:None/"
    assert all(v.startswith("https:80") for v in visitadas[1:])


def test_si_la_ip_no_responde_lo_dice():
    def caida(_request):
        raise httpx.ConnectTimeout("sin respuesta")

    original = httpx.AsyncClient
    huawei.httpx.AsyncClient = lambda **kw: original(transport=httpx.MockTransport(caida), **kw)
    try:
        assert asyncio.run(huawei.leer_serial("10.20.0.9"))["error"] == "la IP no respondió"
    finally:
        huawei.httpx.AsyncClient = original
    assert asyncio.run(huawei.leer_serial(""))["error"] == "sin IP"
