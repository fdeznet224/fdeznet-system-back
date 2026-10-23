"""Red del cliente: equipos y WiFi por TR-069, bloqueo por la web de la ONU Huawei."""

import asyncio
import json

import httpx
import pytest

from src.application.services import onu_web_huawei as web
from src.application.services import red_cliente_service as red
from src.application.services.onu_web_huawei import FiltroMacHuawei, filtro_en_pagina, normalizar_mac
from src.application.services.red_cliente_service import RedClienteService, equipos, redes_wifi, serial_tr069


def pagina_filtro(activo="0", modo="0", macs=()):
    filas = ",".join(f'new stMacFilter("InternetGatewayDevice.X_HW_Security.MacFilter.{i}","{m.replace(":", chr(92) + "x3a")}")'
                     for i, m in enumerate(macs, start=1))
    return f"var enableFilter = '{activo}';\nvar Mode = '{modo}';\nvar MacFilter = new Array({filas + ',' if filas else ''}null);"


def test_serial_huawei_como_lo_reporta_tr069():
    assert serial_tr069("HWTCF1C8D69E") == "48575443F1C8D69E"
    assert serial_tr069("hwtc-f1c8d69e") == "48575443F1C8D69E"
    # Una MAC EPON no es un serial GPON.
    assert serial_tr069("E0:CC:7A:CE:04:AC") is None


def test_mac_en_cualquier_formato():
    assert normalizar_mac("E0-CC-7A-CE-04-AC") == "e0:cc:7a:ce:04:ac"
    assert normalizar_mac("e0cc.7ace.04ac") == "e0:cc:7a:ce:04:ac"
    with pytest.raises(ValueError):
        normalizar_mac("e0:cc:7a")


def test_lee_el_filtro_de_la_pagina_con_escapes():
    estado = filtro_en_pagina(pagina_filtro("1", "0", ["08:65:f0:5e:1f:7e"]))
    assert estado == {"activo": True, "lista_negra": True,
                      "macs": {"08:65:f0:5e:1f:7e": "InternetGatewayDevice.X_HW_Security.MacFilter.1"}}
    with pytest.raises(ValueError):
        filtro_en_pagina("<html>login</html>")


LAN = {
    "Hosts": {"Host": {
        "1": {"HostName": {"_value": "Galaxy"}, "MACAddress": {"_value": "7a:49:bf:ef:ce:78"},
              "IPAddress": {"_value": "192.168.100.7"}, "Active": {"_value": True}, "InterfaceType": {"_value": "802.11"}},
        "2": {"HostName": {"_value": ""}, "MACAddress": {"_value": "08:65:f0:5e:1f:7e"},
              "IPAddress": {"_value": "192.168.100.10"}, "Active": {"_value": False}, "InterfaceType": {"_value": "Ethernet"}},
    }},
    "WLANConfiguration": {
        "1": {"SSID": {"_value": "Casa"}, "X_HW_RFBand": {"_value": "2.4GHz"}, "Enable": {"_value": True},
              "KeyPassphrase": {"_value": ""},
              "AssociatedDevice": {"1": {"AssociatedDeviceMACAddress": {"_value": "7A:49:BF:EF:CE:78"}}}},
        "5": {"SSID": {"_value": "Casa"}, "X_HW_RFBand": {"_value": "5GHz"}, "Enable": {"_value": True}},
        "4": {"SSID": {"_value": ""}},
    },
}


def test_equipos_con_su_red_y_bloqueados_que_ya_no_aparecen():
    redes = redes_wifi(LAN)
    assert [(r["indice"], r["ssid"], r["banda"]) for r in redes] == [(1, "Casa", "2.4GHz"), (5, "Casa", "5GHz")]
    lista = equipos(LAN, redes, {"08:65:f0:5e:1f:7e", "aa:aa:aa:aa:aa:aa"})
    assert [(e["mac"], e["conectado"], e["bloqueado"]) for e in lista] == [
        ("7a:49:bf:ef:ce:78", True, False),
        ("08:65:f0:5e:1f:7e", False, True),
        ("aa:aa:aa:aa:aa:aa", False, True),
    ]
    assert lista[0]["red"] == "Casa · 2.4GHz" and lista[1]["conexion"] == "cable"
    # Sin la web de la ONU no se sabe quién está bloqueado.
    assert all(e["bloqueado"] is None for e in equipos(LAN, redes, None))


class OnuFalsa:
    """La web del HG8145: guarda lo que le mandan los formularios."""

    def __init__(self, activo="0", modo="0", macs=()):
        self.activo, self.modo, self.macs, self.envios = activo, modo, list(macs), []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        ruta = request.url.path
        datos = dict(httpx.QueryParams(request.content.decode())) if request.method == "POST" else {}
        if ruta.endswith("macfilter.asp"):
            return httpx.Response(200, text=pagina_filtro(self.activo, self.modo, self.macs))
        if ruta.endswith("tr069.asp"):
            return httpx.Response(200, text='<input id="onttoken" type="hidden" value="tok123">')
        if ruta in ("/set.cgi", "/add.cgi", "/del.cgi"):
            assert datos.pop("x.X_HW_Token") == "tok123"
            self.envios.append((ruta, datos))
            if ruta == "/set.cgi":
                self.modo = datos.get("x.MacFilterPolicy", self.modo)
                self.activo = datos.get("x.MacFilterRight", self.activo)
            elif ruta == "/add.cgi":
                self.macs.append(datos["x.SourceMACAddress"])
            else:
                quitar = {int(k.rsplit(".", 1)[1]) for k in datos}
                self.macs = [m for i, m in enumerate(self.macs, start=1) if i not in quitar]
        return httpx.Response(200, text="ok")


def con_onu(monkeypatch, onu):
    async def direccion(_ip, _timeout):
        return "https://10.10.10.254:80"

    original = httpx.AsyncClient

    def cliente(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(onu)
        return original(*args, **kwargs)

    monkeypatch.setattr(web, "_direccion_web", direccion)
    monkeypatch.setattr(web.httpx, "AsyncClient", cliente)


def test_bloquear_enciende_el_filtro_en_lista_negra_y_desbloquear_lo_apaga(monkeypatch):
    # La ONU tenía guardada la lista blanca apagada: encenderla así cortaría a todos.
    onu = OnuFalsa(activo="0", modo="1")
    con_onu(monkeypatch, onu)

    async def probar():
        async with FiltroMacHuawei("10.10.10.254", [("telecomadmin", "x")]) as filtro:
            await filtro.bloquear("08-65-F0-5E-1F-7E")
            assert (onu.activo, onu.modo, onu.macs) == ("1", "0", ["08:65:f0:5e:1f:7e"])
            await filtro.desbloquear("08:65:f0:5e:1f:7e")

    asyncio.run(probar())
    assert onu.envios[0] == ("/set.cgi", {"x.MacFilterPolicy": "0", "x.MacFilterRight": "1"})
    assert (onu.activo, onu.macs) == ("0", [])


def test_no_se_toca_el_encendido_si_quedan_otros_bloqueados(monkeypatch):
    onu = OnuFalsa(activo="1", modo="0", macs=["aa:aa:aa:aa:aa:aa", "08:65:f0:5e:1f:7e"])
    con_onu(monkeypatch, onu)

    async def probar():
        async with FiltroMacHuawei("10.10.10.254", [("telecomadmin", "x")]) as filtro:
            await filtro.desbloquear("08:65:f0:5e:1f:7e")

    asyncio.run(probar())
    assert onu.macs == ["aa:aa:aa:aa:aa:aa"] and onu.activo == "1"
    assert [ruta for ruta, _ in onu.envios] == ["/del.cgi"]


def nbi(respuestas):
    llamadas = []

    def manejar(request: httpx.Request) -> httpx.Response:
        llamadas.append((request.method, request.url.path))
        return respuestas(request)

    return httpx.AsyncClient(base_url="http://nbi", transport=httpx.MockTransport(manejar)), llamadas


def test_tarea_rechazada_se_quita_de_la_cola():
    def respuestas(request):
        if request.url.path.endswith("/tasks"):
            return httpx.Response(202, json={"_id": "t1"})
        if request.url.path == "/faults":
            return httpx.Response(200, json=[{"_id": "dev-x:task_t1", "channel": "task_t1",
                                              "detail": {"faultString": "Invalid parameter value"}}])
        return httpx.Response(200)

    async def probar():
        http, llamadas = nbi(respuestas)
        with pytest.raises(ValueError, match="Invalid parameter value"):
            await RedClienteService(None, nbi="http://nbi")._tarea(http, "dev-x", {"name": "setParameterValues"})
        return llamadas

    llamadas = asyncio.run(probar())
    assert ("DELETE", "/tasks/t1") in llamadas and ("DELETE", "/faults/dev-x:task_t1") in llamadas


def test_tarea_sin_respuesta_queda_en_cola():
    def respuestas(request):
        if request.url.path.endswith("/tasks"):
            return httpx.Response(202, json={"_id": "t1"})
        return httpx.Response(200, json=[])

    async def probar():
        http, _ = nbi(respuestas)
        return await RedClienteService(None, nbi="http://nbi")._tarea(http, "dev-x", {"name": "refreshObject"})

    assert asyncio.run(probar()) is False


def test_busca_la_onu_por_serial_y_luego_por_ip():
    consultas = []

    def respuestas(request):
        consulta = json.loads(request.url.params["query"])
        consultas.append(consulta)
        return httpx.Response(200, json=[{"_id": "00259E-HG8145V5-X"}] if len(consultas) == 2 else [])

    async def probar():
        http, _ = nbi(respuestas)
        return await RedClienteService(None, nbi="http://nbi")._id_tr069(http, {"identificador": "HWTCF1C8D69E", "ip": "10.10.10.254"})

    assert asyncio.run(probar()) == "00259E-HG8145V5-X"
    assert set(consultas[0]["_deviceId._SerialNumber"]["$in"]) == {"HWTCF1C8D69E", "48575443F1C8D69E"}
    assert "10\\.10\\.10\\.254" in list(consultas[1].values())[0]["$regex"]


def test_clave_wifi_valida_antes_de_tocar_la_onu():
    servicio = RedClienteService(None, nbi="http://nbi")
    for mala in ("corta", "contraseña-con-ñ", "x" * 64):
        with pytest.raises(ValueError, match="8 a 63"):
            asyncio.run(servicio.cambiar_clave_wifi(1, "Casa", mala))


def test_nbi_configurable(monkeypatch):
    monkeypatch.delenv("GENIEACS_NBI_URL", raising=False)
    assert red.url_nbi() == "http://127.0.0.1:7557"
    monkeypatch.setenv("GENIEACS_NBI_URL", "http://10.0.0.5:7557/")
    assert red.url_nbi() == "http://10.0.0.5:7557"
