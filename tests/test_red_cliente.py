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
    """Página de un filtro; cada mac es "aa:..." (cable) o ("SSID-1", "aa:...") (WiFi)."""
    filas = []
    for i, m in enumerate(macs, start=1):
        red, mac = m if isinstance(m, tuple) else (None, m)
        campos = [f"InternetGatewayDevice.X_HW_Security.Filtro.{i}"] + ([red] if red else []) + [mac.replace(":", chr(92) + "x3a")]
        filas.append("new stMacFilter(" + ",".join(f'"{c}"' for c in campos) + ")")
    return (f"var enableFilter = '{activo}';\nvar Mode = '{modo}';\nvar MacFilter = new Array({','.join(filas) + ',' if filas else ''}null);"
            '<input type="hidden" name="onttoken" id="hwonttoken" value="abc123">')


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
    assert estado["activo"] and estado["lista_negra"]
    assert estado["macs"] == {"08:65:f0:5e:1f:7e": ["InternetGatewayDevice.X_HW_Security.Filtro.1"]}
    # El de WiFi trae la red entre el dominio y la MAC.
    wifi = filtro_en_pagina(pagina_filtro("1", "0", [("SSID-5", "aa:14:54:0d:2d:55")]))
    assert wifi["entradas"] == [("InternetGatewayDevice.X_HW_Security.Filtro.1", "aa:14:54:0d:2d:55", "SSID-5")]
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


class Filtro:
    def __init__(self, activo="0", modo="0", macs=()):
        self.activo, self.modo, self.macs = activo, modo, list(macs)


class OnuFalsa:
    """La web del HG8145 con sus dos filtros (cable y WiFi): guarda lo que mandan los formularios."""

    def __init__(self, cable=None, wifi=None):
        self.cable, self.wifi, self.envios = cable or Filtro(), wifi or Filtro(), []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        ruta = request.url.path
        datos = dict(httpx.QueryParams(request.content.decode())) if request.method == "POST" else {}
        if ruta.endswith("wlanmacfilter.asp"):
            return httpx.Response(200, text=pagina_filtro(self.wifi.activo, self.wifi.modo, self.wifi.macs))
        if ruta.endswith("macfilter.asp"):
            return httpx.Response(200, text=pagina_filtro(self.cable.activo, self.cable.modo, self.cable.macs))
        if ruta in ("/set.cgi", "/add.cgi", "/del.cgi"):
            assert datos.pop("x.X_HW_Token") == "abc123"
            wifi = "wlanmacfilter" in request.url.params.get("RequestFile", "")
            filtro = self.wifi if wifi else self.cable
            self.envios.append(("wifi" if wifi else "cable", ruta, datos))
            if ruta == "/set.cgi":
                filtro.modo = datos.get("x.WlanMacFilterPolicy" if wifi else "x.MacFilterPolicy", filtro.modo)
                filtro.activo = datos.get("x.WlanMacFilterRight" if wifi else "x.MacFilterRight", filtro.activo)
            elif ruta == "/add.cgi":
                mac = datos["x.SourceMACAddress"]
                filtro.macs.append((datos["x.SSIDName"], mac) if wifi else mac)
            else:
                quitar = {int(k.rsplit(".", 1)[1]) for k in datos}
                filtro.macs = [m for i, m in enumerate(filtro.macs, start=1) if i not in quitar]
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


def test_bloquear_pone_la_mac_en_cable_y_en_cada_banda_del_wifi(monkeypatch):
    # Los dos filtros tenían guardada la lista blanca apagada: encenderla así cortaría a todos.
    onu = OnuFalsa(Filtro("0", "1"), Filtro("0", "1"))
    con_onu(monkeypatch, onu)

    async def probar():
        async with FiltroMacHuawei("10.10.10.254", [("root", "admin")]) as filtro:
            estado = await filtro.bloquear("AA-14-54-0D-2D-55", [1, 5])
            assert estado["bloqueadas"] == {"aa:14:54:0d:2d:55"}
            await filtro.desbloquear("aa:14:54:0d:2d:55")

    asyncio.run(probar())
    sets = [(f, d) for f, r, d in onu.envios if r == "/set.cgi"]
    assert ("cable", {"x.MacFilterPolicy": "0", "x.MacFilterRight": "1"}) in sets
    assert ("wifi", {"x.WlanMacFilterPolicy": "0", "x.WlanMacFilterRight": "1"}) in sets
    altas = [d for f, r, d in onu.envios if f == "wifi" and r == "/add.cgi"]
    assert [a["x.SSIDName"] for a in altas] == ["SSID-1", "SSID-5"]
    # Al quitar la última, se apagan los dos filtros como estaban.
    assert (onu.cable.activo, onu.cable.macs, onu.wifi.activo, onu.wifi.macs) == ("0", [], "0", [])


def test_no_se_toca_el_encendido_si_quedan_otros_bloqueados(monkeypatch):
    otro = "bb:bb:bb:bb:bb:bb"
    onu = OnuFalsa(Filtro("1", "0", [otro, "08:65:f0:5e:1f:7e"]),
                   Filtro("1", "0", [("SSID-1", otro), ("SSID-1", "08:65:f0:5e:1f:7e"), ("SSID-5", "08:65:f0:5e:1f:7e")]))
    con_onu(monkeypatch, onu)

    async def probar():
        async with FiltroMacHuawei("10.10.10.254", [("root", "admin")]) as filtro:
            await filtro.desbloquear("08:65:f0:5e:1f:7e")

    asyncio.run(probar())
    assert onu.cable.macs == [otro] and onu.wifi.macs == [("SSID-1", otro)]
    assert onu.cable.activo == "1" and onu.wifi.activo == "1"
    assert [r for _, r, _ in onu.envios] == ["/del.cgi", "/del.cgi"]


def test_bloquea_en_las_bandas_de_la_red_donde_esta_el_equipo():
    mac_fichas = "ea:9d:ce:f0:2d:1e"
    assert red.redes_para_bloquear(LAN_CON_FICHAS, "7a:49:bf:ef:ce:78", True) == [1, 5]
    # Desde el portal no se bloquea al de la red de fichas; desde la ficha sí, en sus dos bandas.
    with pytest.raises(ValueError, match="no está en tu red"):
        red.redes_para_bloquear(LAN_CON_FICHAS, mac_fichas, True)
    assert red.redes_para_bloquear(LAN_CON_FICHAS, mac_fichas, False) == [2, 3]
    # Uno que la ONU no conoce: la red principal.
    assert red.redes_para_bloquear(LAN_CON_FICHAS, "cc:cc:cc:cc:cc:cc", False) == [1, 5]


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


LAN_CON_FICHAS = {
    "Hosts": {"Host": {
        "1": {"HostName": {"_value": "Celular"}, "MACAddress": {"_value": "7a:49:bf:ef:ce:78"}, "Active": {"_value": True},
              "InterfaceType": {"_value": "802.11"}, "Layer2Interface": {"_value": "InternetGatewayDevice.LANDevice.1.WLANConfiguration.5"}},
        "2": {"HostName": {"_value": "De fichas"}, "MACAddress": {"_value": "ea:9d:ce:f0:2d:1e"}, "Active": {"_value": False},
              "InterfaceType": {"_value": "802.11"}, "Layer2Interface": {"_value": "InternetGatewayDevice.LANDevice.1.WLANConfiguration.2"}},
        "3": {"HostName": {"_value": "TV"}, "MACAddress": {"_value": "84:d0:db:f5:c1:20"}, "Active": {"_value": True},
              "InterfaceType": {"_value": "Ethernet"}, "Layer2Interface": {"_value": "InternetGatewayDevice.LANDevice.1.LANEthernetInterfaceConfig.1"}},
    }},
    "WLANConfiguration": {
        "1": {"SSID": {"_value": "FdezNet-187B"}, "X_HW_RFBand": {"_value": "2.4GHz"}},
        "2": {"SSID": {"_value": "Zona FdezNet"}, "X_HW_RFBand": {"_value": "2.4GHz"}},
        "3": {"SSID": {"_value": "Zona FdezNet"}, "X_HW_RFBand": {"_value": "5GHz"}},
        "5": {"SSID": {"_value": "FdezNet-187B"}, "X_HW_RFBand": {"_value": "5GHz"}},
    },
}


def test_el_portal_solo_ve_su_red_principal_y_no_la_de_fichas():
    redes = redes_wifi(LAN_CON_FICHAS)
    assert red.indices_principales(redes) == {1, 5}
    suyas, lista = red.solo_de_la_red_principal(redes, equipos(LAN_CON_FICHAS, redes, set()))
    assert {r["ssid"] for r in suyas} == {"FdezNet-187B"}
    # El de fichas se fue aunque ya está desconectado (se sabe por Layer2Interface); el de cable se queda.
    assert [e["nombre"] for e in lista] == ["Celular", "TV"]
    assert lista[0]["red"] == "FdezNet-187B · 5GHz"


def test_el_nombre_que_le_puso_el_cliente_va_con_el_equipo_aunque_este_bloqueado():
    redes = redes_wifi(LAN)
    lista = equipos(LAN, redes, {"aa:aa:aa:aa:aa:aa"}, {"7a:49:bf:ef:ce:78": "Celular de Juan", "aa:aa:aa:aa:aa:aa": "Tablet"})
    assert {e["mac"]: e["apodo"] for e in lista} == {
        "7a:49:bf:ef:ce:78": "Celular de Juan", "08:65:f0:5e:1f:7e": None, "aa:aa:aa:aa:aa:aa": "Tablet",
    }
    # El nombre de la ONU se queda: el portal muestra los dos.
    assert lista[0]["nombre"] == "Galaxy"


class NombresFalsos:
    def __init__(self):
        self.filas = {}

    async def get(self, modelo, llave):
        return self.filas.get(llave)

    def add(self, fila):
        self.filas[(fila.cliente_id, fila.mac)] = fila

    async def delete(self, fila):
        self.filas.pop((fila.cliente_id, fila.mac))

    async def commit(self):
        pass


def test_nombrar_guarda_por_mac_cambia_y_vacio_lo_quita():
    db = NombresFalsos()
    servicio = RedClienteService(db)
    assert asyncio.run(servicio.nombrar(327, "7A-49-BF-EF-CE-78", "  Celular   de Juan ")) == {"mac": "7a:49:bf:ef:ce:78", "apodo": "Celular de Juan"}
    assert db.filas[(327, "7a:49:bf:ef:ce:78")].nombre == "Celular de Juan"
    asyncio.run(servicio.nombrar(327, "7a:49:bf:ef:ce:78", "Juan"))
    assert db.filas[(327, "7a:49:bf:ef:ce:78")].nombre == "Juan"
    assert asyncio.run(servicio.nombrar(327, "7a:49:bf:ef:ce:78", " "))["apodo"] is None
    assert db.filas == {}
    with pytest.raises(ValueError, match="hasta 40"):
        asyncio.run(servicio.nombrar(327, "7a:49:bf:ef:ce:78", "x" * 41))
    with pytest.raises(ValueError):
        asyncio.run(servicio.nombrar(327, "no-es-mac", "Juan"))
