"""Red inalámbrica: lectura de airOS, clientes por AP y árbol."""

import asyncio
from types import SimpleNamespace

import httpx

import src.application.services.red_inalambrica_service as red
from src.infrastructure.models import ClienteModel, EquipoRedModel

STATUS_V8 = {
    "host": {"hostname": "AP-Norte", "fwversion": "WA.v8.7.11", "devmodel": "LiteAP AC", "uptime": 86400},
    "wireless": {"mode": "ap-ptmp", "essid": "FDEZ-NORTE", "frequency": 5745, "chanbw": 40, "signal": -52,
                 "noisef": -94, "ccq": 91, "count": 2, "polling": {"dl_capacity": 180000, "ul_capacity": 120000}},
}
STA_V8 = [
    {"mac": "fc:ec:da:11:22:33", "lastip": "10.50.0.11", "signal": -61, "remote": {"hostname": "Juan"}},
    {"mac": "fc:ec:da:44:55:66", "lastip": "10.50.0.12", "signal": -78},
]


def test_resume_el_status_y_las_estaciones():
    status = red.resumir_status(STATUS_V8)
    assert status["senal"] == -52 and status["ruido"] == -94 and status["ccq"] == 91
    assert status["frecuencia"] == "5745 MHz" and status["ancho_canal"] == "40" and status["estaciones"] == 2
    assert status["capacidad_tx"] == 180000 and status["modelo"] == "LiteAP AC" and status["uptime_s"] == 86400
    estaciones = red.resumir_estaciones(STA_V8)
    assert estaciones[0] == {"mac": "FC:EC:DA:11:22:33", "ip": "10.50.0.11", "senal": -61, "ccq": None, "nombre": "Juan"}
    # airOS 6 manda la frecuencia como texto.
    assert red.resumir_status({"wireless": {"frequency": "5800 MHz", "signal": "-60"}})["frecuencia"] == "5800 MHz"


def _airos(version: int):
    def responder(peticion: httpx.Request) -> httpx.Response:
        ruta = peticion.url.path
        if ruta == "/api/auth":
            if version == 8:
                return httpx.Response(200, json={"ok": True}, headers={"X-CSRF-ID": "abc", "set-cookie": "AIROS_X=1; Path=/"})
            return httpx.Response(404)
        if ruta == "/login.cgi":
            return httpx.Response(200, text="ok", headers={"set-cookie": "AIROS_SESSIONID=9; Path=/"})
        if ruta == "/status.cgi":
            if "AIROS" not in peticion.headers.get("cookie", ""):
                return httpx.Response(302, headers={"location": "/login.cgi"})
            if version == 8:
                assert peticion.headers.get("X-CSRF-ID") == "abc"
            return httpx.Response(200, json=STATUS_V8)
        if ruta == "/sta.cgi":
            return httpx.Response(200, json=STA_V8)
        return httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(responder), follow_redirects=False)


def test_lee_airos_8_y_airos_6():
    for version in (8, 6):
        datos = asyncio.run(red.leer_airos("https://10.0.0.2", "ubnt", "clave", http=_airos(version)))
        assert datos["status"]["senal"] == -52 and len(datos["estaciones"]) == 2


class _Resultado:
    def __init__(self, valores):
        self.valores = valores

    def scalars(self):
        return self

    def all(self):
        return self.valores


class _DB:
    def __init__(self, clientes, equipos):
        self.clientes, self.equipos = clientes, equipos

    async def execute(self, sentencia):
        texto = str(sentencia)
        return _Resultado(self.clientes if "FROM clientes" in texto else self.equipos)


def test_acomoda_los_clientes_en_su_ap_por_ip_o_mac():
    ap = EquipoRedModel(id=5, nombre="AP Norte", ip="10.50.0.1")
    estacion = EquipoRedModel(id=6, nombre="PtP Sagrado", ip="10.50.0.12", padre_id=None)
    juan = ClienteModel(id=1, nombre="Juan", ip_antena="10.50.0.11", mac_address=None, tipo_conexion="radio")
    ana = ClienteModel(id=2, nombre="Ana", ip_antena="10.99.0.9", mac_address="fc:ec:da:44:55:66", tipo_conexion="radio")
    lejano = ClienteModel(id=3, nombre="Otro", ip_antena="10.60.0.1", mac_address=None, tipo_conexion="radio")
    servicio = red.RedInalambricaService(_DB([juan, ana, lejano], [estacion]))
    asyncio.run(servicio._vincular_estaciones(ap, red.resumir_estaciones(STA_V8)))
    assert (juan.equipo_red_id, juan.antena_senal) == (5, -61)
    assert (ana.equipo_red_id, ana.antena_senal) == (5, -78)
    assert lejano.equipo_red_id is None
    assert estacion.padre_id == 5  # el PtP estación que se ve en la base cuelga de ella


def test_puente_de_equipo_vence():
    from datetime import datetime, timedelta

    ahora = datetime(2026, 11, 25, 10, 0)
    assert red.puente_equipo_vencido(red.comentario_equipo(4, ahora - timedelta(minutes=1)), ahora)
    assert not red.puente_equipo_vencido(red.comentario_equipo(4, ahora + timedelta(minutes=5)), ahora)
    assert not red.puente_equipo_vencido("FDEZNET-ANTENA 4 2026-11-25T09:00", ahora)  # el de las antenas no es suyo


def test_el_arbol_cuelga_equipos_y_clientes(monkeypatch):
    router = SimpleNamespace(id=2, nombre="Vicente Guerrero", ip_vpn="10.8.0.2", is_online=True)
    base = EquipoRedModel(id=1, nombre="PtP VG", tipo="ptp_base", marca="ubiquiti", ip="10.1.0.1", router_id=2, padre_id=None, activo=True)
    ap = EquipoRedModel(id=2, nombre="AP Sagrado", tipo="ap", marca="ubiquiti", ip="10.1.0.3", router_id=2, padre_id=1, activo=True)
    juan = ClienteModel(id=1, cedula="4A1B", nombre="Juan", estado="activo", ip_antena="10.50.0.11", equipo_red_id=2, router_id=2)
    ana = ClienteModel(id=2, cedula="7C2D", nombre="Ana", estado="activo", ip_antena="10.50.0.13", equipo_red_id=None, router_id=2)

    class _DBArbol:
        async def execute(self, sentencia):
            texto = str(sentencia)
            if "FROM routers" in texto:
                return _Resultado([router])
            if "FROM equipos_red" in texto:
                return _Resultado([base, ap])
            return _Resultado([juan, ana])

    arbol = asyncio.run(red.RedInalambricaService(_DBArbol()).arbol())
    vg = arbol["routers"][0]
    assert vg["nombre"] == "Vicente Guerrero" and [e["nombre"] for e in vg["equipos"]] == ["PtP VG"]
    assert vg["equipos"][0]["hijos"][0]["nombre"] == "AP Sagrado"
    assert [c["contrato"] for c in vg["equipos"][0]["hijos"][0]["clientes"]] == ["4A1B"]
    assert [c["contrato"] for c in vg["clientes_sin_ap"]] == ["7C2D"]
