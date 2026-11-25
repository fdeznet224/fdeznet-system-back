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
    assert red.resumir_estaciones([{"mac": "x", "lastip": "0.0.0.0"}])[0]["ip"] is None
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
                return httpx.Response(200, json={**STATUS_V8, "wireless": {**STATUS_V8["wireless"], "sta": STA_V8}})
            return httpx.Response(200, json=STATUS_V8)
        if ruta == "/sta.cgi":
            assert version == 6, "airOS 8 trae las estaciones en status.cgi"
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
            if "FROM olts" in texto:
                return _Resultado([])
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


def test_empareja_por_nombre_si_la_antena_no_trae_ip():
    ap = EquipoRedModel(id=7, nombre="FdezNet-3-AUX", ip="13.13.13.5", router_id=4)
    flor = ClienteModel(id=4, nombre="Flor Ruiz Hernández", ip_antena="13.13.13.17", mac_address=None, tipo_conexion="radio")
    ximena = ClienteModel(id=5, nombre="Ximena Zuñiga Sanchez", ip_antena="13.13.13.99", mac_address=None, tipo_conexion="radio")
    estaciones = red.resumir_estaciones([
        {"mac": "74:AC:B9:72:BF:AC", "lastip": "0.0.0.0", "signal": -57, "remote": {"hostname": "Flor Ruiz Hernandez"}},
        {"mac": "60:22:32:B2:D1:F4", "lastip": "13.13.13.26", "signal": -63, "remote": {"hostname": "Ximena Zuniga"}},
    ])
    asyncio.run(red.RedInalambricaService(_DB([flor, ximena], []))._vincular_estaciones(ap, estaciones))
    assert (flor.equipo_red_id, flor.antena_senal) == (7, -57)
    assert (ximena.equipo_red_id, ximena.antena_senal) == (7, -63)  # "ximena zuniga" está dentro de su nombre
    assert red.mismo_nombre("Juan", "Juan Pérez") is False  # una sola palabra no basta


def test_la_troncal_va_arriba_y_el_mikrotik_cuelga_de_su_enlace():
    fm = SimpleNamespace(id=4, nombre="Flores Magon", ip_vpn="10.8.0.4", is_online=True)
    vg = SimpleNamespace(id=2, nombre="Vicente Guerrero", ip_vpn="10.8.0.3", is_online=True)
    base = dict(marca="ubiquiti", activo=True, router_hijo_id=None)
    nodo = EquipoRedModel(id=1, nombre="RB-ADMINISTRADOR", tipo="router", ip="192.168.21.1", router_id=4, padre_id=None, troncal=True, **{**base, "marca": "mikrotik"})
    af = EquipoRedModel(id=2, nombre="airFiber nodo", tipo="ptp_base", ip="192.168.21.2", router_id=4, padre_id=1, troncal=True, **base)
    mimosa = EquipoRedModel(id=3, nombre="Mimosa FM", tipo="ptp_estacion", ip="192.168.21.7", router_id=4, padre_id=2, troncal=True, **{**base, "marca": "mimosa", "router_hijo_id": 4})
    ap = EquipoRedModel(id=4, nombre="FdezNet-90-sec2", tipo="ap", ip="13.13.13.3", router_id=4, padre_id=None, troncal=False, **base)
    juan = ClienteModel(id=1, cedula="3067", nombre="Ximena", estado="activo", ip_antena="13.13.13.26", equipo_red_id=4, router_id=4)
    ana = ClienteModel(id=2, cedula="7C2D", nombre="Ana", estado="activo", ip_antena="10.50.0.13", equipo_red_id=None, router_id=2)

    class _DBArbol:
        async def execute(self, sentencia):
            texto = str(sentencia)
            if "FROM olts" in texto:
                return _Resultado([])
            if "FROM routers" in texto:
                return _Resultado([fm, vg])
            if "FROM equipos_red" in texto:
                return _Resultado([nodo, af, mimosa, ap])
            return _Resultado([juan, ana])

    arbol = asyncio.run(red.RedInalambricaService(_DBArbol()).arbol())
    raiz = arbol["troncal"][0]
    assert raiz["nombre"] == "RB-ADMINISTRADOR"
    enlace = raiz["hijos"][0]["hijos"][0]
    assert enlace["nombre"] == "Mimosa FM" and enlace["router"]["nombre"] == "Flores Magon"
    assert [e["nombre"] for e in enlace["router"]["equipos"]] == ["FdezNet-90-sec2"]
    assert enlace["router"]["equipos"][0]["clientes"][0]["contrato"] == "3067"
    # Flores Magón ya no sale suelto; Vicente Guerrero sí (aún no cuelga de su enlace).
    assert [r["nombre"] for r in arbol["routers"]] == ["Vicente Guerrero"]


def test_la_olt_agrupa_a_sus_clientes_por_pon():
    from decimal import Decimal

    olt = SimpleNamespace(id=8, nombre="Flores Magon", ip="13.13.13.2", tecnologia="GPON", router_id=4)
    ana = ClienteModel(id=1, cedula="1A2B", nombre="Ana", estado="activo", olt_id=8, is_online=True)
    luis = ClienteModel(id=2, cedula="3C4D", nombre="Luis", estado="activo", olt_id=8, is_online=False)
    sin = ClienteModel(id=3, cedula="5E6F", nombre="Sin lectura", estado="activo", olt_id=8, is_online=None)
    lecturas = [SimpleNamespace(cliente_id=1, pon=2, potencia_rx_dbm=Decimal("-21.5")),
                SimpleNamespace(cliente_id=2, pon=1, potencia_rx_dbm=Decimal("-28.1"))]

    class _DBOlt:
        async def execute(self, sentencia):
            texto = str(sentencia)
            if "FROM olts" in texto:
                return _Resultado([olt])
            if "FROM lecturas_opticas" in texto:
                return _Resultado(lecturas)
            return _Resultado([ana, luis, sin])

    olts = asyncio.run(red.RedInalambricaService(_DBOlt())._olts_con_clientes())
    pons = olts[0]["pons"]
    assert [p["pon"] for p in pons] == [1, 2, None]
    assert pons[0]["clientes"][0] == {"id": 2, "contrato": "3C4D", "nombre": "Luis", "estado": "activo", "responde": False, "potencia": -28.1}
    assert pons[1]["clientes"][0]["potencia"] == -21.5 and pons[2]["clientes"][0]["responde"] is None
