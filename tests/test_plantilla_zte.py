"""La ZTE que entra al ACS queda con la WAN y la red de fichas, como la Huawei del almacén."""

import asyncio
import json

import httpx

from src.application.services import plantilla_zte_service as plantilla
from src.application.services.plantilla_zte_service import PlantillaZteService, lo_que_falta
from src.application.services.red_cliente_service import (
    RedClienteService, bloqueadas_zte, prefijo_zte, redes_wifi,
)

IGD = "InternetGatewayDevice"
CONFIG = {"activa": True, "nombre": "Zona FdezNet", "vlan": 4, "lan": [4]}


def v(valor):
    return {"_value": valor, "_writable": True}


def wlan(ssid, encendida, banda, prefijo):
    return {"SSID": v(ssid), "Enable": v(encendida), f"{prefijo}_OperatingFrequencyBand": v(banda),
            "BeaconType": v("11i"), f"{prefijo}_AccessControlMode": v("Disabled"), f"{prefijo}_AccessControl": {}}


def zte(prefijo="X_TELMEX_COM", fichas=True, puente_pppoe=False):
    """Una F670L recién restablecida: PPPoE de instalación y ACS, sin WAN de fichas ni enlaces."""
    ppp = {"2": {"ConnectionType": v("IP_Routed"), "Name": v("Internet"), "Username": v("FdezNet"),
                 f"{prefijo}_ServiceList": v("INTERNET_TR069"), f"{prefijo}_VLANID": v(10)}}
    if puente_pppoe:
        ppp["1024"] = {"ConnectionType": v("PPPoE_Bridged"), "Name": v("Zona FdezNet"),
                       f"{prefijo}_ServiceList": v("INTERNET"), f"{prefijo}_VLANID": v(0)}
    redes = {str(i): wlan(f"SSID{i}", i in (1, 5), "2.4GHz" if i <= 4 else "5GHz", prefijo) for i in range(1, 9)}
    redes["1"]["SSID"] = redes["5"]["SSID"] = v("FdezNet-")
    if fichas:
        for i in ("2", "6"):
            redes[i].update(SSID=v("Zona FdezNet"), Enable=v(True))
    return {IGD: {
        "WANDevice": {"1": {"WANConnectionDevice": {"1": {"WANPPPConnection": ppp, "WANIPConnection": {}},
                                                    "_timestamp": "2026-11-04T07:41:00.000Z"}}},
        "LANDevice": {"1": {"WLANConfiguration": redes}},
        f"{prefijo}_PortBinding": {},
    }, "_lastBoot": "2026-11-04T07:37:50.985Z"}


class AcsFalso:
    """GenieACS con una ZTE que contesta al instante. `rechaza(tarea)` simula las mañas del firmware."""

    def __init__(self, documento, rechaza=lambda tarea: False):
        self.doc, self.rechaza, self.tareas, self.fallas = documento, rechaza, [], []

    def _nodo(self, ruta, crear=False):
        nodo = self.doc
        for parte in ruta.split("."):
            if parte not in nodo:
                if not crear:
                    return None
                nodo[parte] = {}
            nodo = nodo[parte]
        return nodo

    def aplicar(self, tarea):
        if tarea["name"] == "addObject":
            objeto = self._nodo(tarea["objectName"], crear=True)
            n = max([int(k) for k in objeto if k.isdigit()] + [0]) + 1
            objeto[str(n)] = ({"WANInterface": v(""), "LANInterface": v("")} if "PortBinding" in tarea["objectName"]
                              else {"MACAddress": v("00:00:00:00:00:00")} if "AccessControl" in tarea["objectName"]
                              else {"ConnectionType": v("Unconfigured"), "Enable": v(False)})
        elif tarea["name"] == "deleteObject":
            padre, _, hijo = tarea["objectName"].rpartition(".")
            del self._nodo(padre)[hijo]
        elif tarea["name"] == "setParameterValues":
            for ruta, valor, _ in tarea["parameterValues"]:
                padre, _, campo = ruta.rpartition(".")
                self._nodo(padre, crear=True)[campo] = v(valor)
            # La WAN puente queda conectada en cuanto se enciende.
            for dispositivo in self.doc[IGD]["WANDevice"]["1"]["WANConnectionDevice"].values():
                for ip in (dispositivo.get("WANIPConnection") or {}).values() if isinstance(dispositivo, dict) else []:
                    if isinstance(ip, dict) and ip.get("Enable", {}).get("_value"):
                        ip["ConnectionStatus"] = v("Connected")

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/tasks"):
            tarea = json.loads(request.content)
            self.tareas.append(tarea)
            if self.rechaza(tarea):
                self.fallas.append({"_id": "dev:task_t", "channel": "task_t", "detail": {"faultString": "Invalid arguments"}})
                return httpx.Response(202, json={"_id": "t"})
            self.aplicar(tarea)
            return httpx.Response(200, json={})
        if request.method == "DELETE":
            self.fallas = []
            return httpx.Response(200)
        if request.url.path == "/faults":
            return httpx.Response(200, json=self.fallas)
        if request.url.path == "/tasks":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[{"_id": "dev", **json.loads(json.dumps(self.doc))}])


def aplicar(acs):
    async def probar():
        async with httpx.AsyncClient(base_url="http://nbi", transport=httpx.MockTransport(acs)) as http:
            return await PlantillaZteService(None, nbi="http://nbi").aplicar(http, "dev", CONFIG)

    return asyncio.run(probar())


def enlaces_de(acs, prefijo="X_TELMEX_COM"):
    return {e["WANInterface"]["_value"].rsplit("1.", 1)[-1]: sorted(e["LANInterface"]["_value"].split(","))
            for e in acs.doc[IGD][f"{prefijo}_PortBinding"].values()}


def test_zte_restablecida_queda_con_la_wan_de_fichas_y_los_dos_enlaces():
    acs = AcsFalso(zte())
    resultado = aplicar(acs)
    assert resultado["faltan"] == [] and "WAN de fichas (puente, VLAN 4)" in resultado["hecho"]
    wan = acs.doc[IGD]["WANDevice"]["1"]["WANConnectionDevice"]["1"]["WANIPConnection"]["1"]
    assert wan["ConnectionType"]["_value"] == "IP_Bridged" and wan["X_TELMEX_COM_VLANID"]["_value"] == 4
    assert wan["X_TELMEX_COM_ServiceList"]["_value"] == "INTERNET"
    lan = "InternetGatewayDevice.LANDevice.1."
    assert enlaces_de(acs) == {
        "WANIPConnection.1": [lan + "LANEthernetInterfaceConfig.4", lan + "WLANConfiguration.2", lan + "WLANConfiguration.6"],
        "WANPPPConnection.2": sorted([lan + f"LANEthernetInterfaceConfig.{p}" for p in (1, 2, 3)]
                                     + [lan + "WLANConfiguration.1", lan + "WLANConfiguration.5"]),
    }


def test_con_el_prefijo_de_fabrica_tambien():
    acs = AcsFalso(zte("X_ZTE-COM"))
    assert aplicar(acs)["faltan"] == []
    assert set(enlaces_de(acs, "X_ZTE-COM")) == {"WANIPConnection.1", "WANPPPConnection.2"}


def test_la_que_ya_esta_completa_no_se_toca():
    acs = AcsFalso(zte())
    aplicar(acs)
    acs.tareas.clear()
    resultado = aplicar(acs)
    assert resultado == {"hecho": [], "avisos": [], "faltan": []}
    assert not [t for t in acs.tareas if t["name"] != "refreshObject"]


def test_quita_la_wan_pppoe_bridged_de_fichas_que_estorba():
    acs = AcsFalso(zte(puente_pppoe=True))
    resultado = aplicar(acs)
    assert "1024" not in acs.doc[IGD]["WANDevice"]["1"]["WANConnectionDevice"]["1"]["WANPPPConnection"]
    assert resultado["faltan"] == []


def test_red_de_fichas_en_la_banda_que_si_enciende_abierta():
    # Como la ZTEG243606C5: la WLAN 2 no enciende y la 3 solo con BeaconType "None".
    def rechaza(tarea):
        valores = {r.split("WLANConfiguration.")[-1]: x for r, x, _ in tarea.get("parameterValues", [])
                   if "WLANConfiguration" in r}
        return valores.get("2.Enable") is True or valores.get("3.BeaconType") == "Basic"

    acs = AcsFalso(zte(fichas=False), rechaza)
    resultado = aplicar(acs)
    redes = acs.doc[IGD]["LANDevice"]["1"]["WLANConfiguration"]
    assert redes["3"]["SSID"]["_value"] == "Zona FdezNet" and redes["3"]["Enable"]["_value"] is True
    assert redes["3"]["BeaconType"]["_value"] == "None" and redes["2"]["Enable"]["_value"] is False
    assert "red de fichas 2.4GHz" in resultado["hecho"] and resultado["faltan"] == []


def test_si_ninguna_red_enciende_queda_como_aviso_y_no_como_falla():
    acs = AcsFalso(zte(fichas=False), lambda t: any(r.endswith(".Enable") and "WLAN" in r
                                                   for r, _, _ in t.get("parameterValues", [])))
    resultado = aplicar(acs)
    assert resultado["faltan"] == [] and len(resultado["avisos"]) == 2


def test_se_vuelve_a_revisar_solo_si_arranco_despues_de_la_ultima_lectura():
    documento = zte()
    assert plantilla.leida_despues_de_arrancar(documento) is True
    documento["_lastBoot"] = "2026-11-04T08:00:00.000Z"
    assert plantilla.leida_despues_de_arrancar(documento) is False
    assert "WAN de fichas" in lo_que_falta(documento[IGD], CONFIG)


def test_la_vuelta_solo_toca_las_que_les_falta_algo():
    acs = AcsFalso(zte())
    aplicar(acs)
    plantilla._ultimo_intento.clear()

    class Db:
        async def execute(self, _):
            from types import SimpleNamespace
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

    acs.tareas.clear()
    conteo = asyncio.run(PlantillaZteService(Db(), nbi="http://nbi", transporte=httpx.MockTransport(acs)).revisar())
    assert conteo == {"revisadas": 0, "configuradas": 0, "errores": 0} and acs.tareas == []


# ------------------------------------------------------------------ portal con ZTE
def test_prefijo_banda_y_bloqueados_de_la_zte():
    lan = zte("X_ZTE-COM")[IGD]["LANDevice"]["1"]
    assert prefijo_zte(lan) == "X_ZTE-COM" and prefijo_zte(zte()[IGD]) == "X_TELMEX_COM"
    assert prefijo_zte({"WLANConfiguration": {"1": {"X_HW_RFBand": v("2.4GHz")}}}) is None
    assert [(r["indice"], r["banda"]) for r in redes_wifi(lan) if r["activa"]] == [(1, "2.4GHz"), (2, "2.4GHz"), (5, "5GHz"), (6, "5GHz")]
    lan["WLANConfiguration"]["1"]["X_ZTE-COM_AccessControl"] = {"1": {"MACAddress": v("AA:BB:CC:DD:EE:01")}}
    assert bloqueadas_zte(lan) == set()  # Con el modo Disabled la lista no bloquea.
    lan["WLANConfiguration"]["1"]["X_ZTE-COM_AccessControlMode"] = v("Ban")
    assert bloqueadas_zte(lan) == {"aa:bb:cc:dd:ee:01"}


def test_si_la_onu_no_contesta_ninguna_banda_queda_a_medias():
    acs = AcsFalso(zte(), lambda t: t["name"] == "setParameterValues")
    servicio = RedClienteService(None, nbi="http://nbi")
    original = httpx.AsyncClient

    async def probar():
        async def id_tr069(_http, _equipo):
            return "dev"

        servicio._id_tr069 = id_tr069
        httpx.AsyncClient = lambda **kw: original(**{**kw, "transport": httpx.MockTransport(acs)})
        try:
            try:
                await servicio._bloquear_zte({"marca": "zte"}, "aa:bb:cc:dd:ee:01", True, [1, 5])
            except ValueError:
                pass
        finally:
            httpx.AsyncClient = original

    asyncio.run(probar())
    redes = acs.doc[IGD]["LANDevice"]["1"]["WLANConfiguration"]
    assert redes["1"]["X_TELMEX_COM_AccessControlMode"]["_value"] == redes["5"]["X_TELMEX_COM_AccessControlMode"]["_value"] == "Disabled"


def test_bloquear_y_desbloquear_en_la_zte_por_tr069():
    acs = AcsFalso(zte())
    servicio = RedClienteService(None, nbi="http://nbi")
    original = httpx.AsyncClient

    async def probar():
        async def id_tr069(_http, _equipo):
            return "dev"

        servicio._id_tr069 = id_tr069
        httpx.AsyncClient = lambda **kw: original(**{**kw, "transport": httpx.MockTransport(acs)})
        try:
            equipo = {"identificador": "ZTEG24284C96", "ip": "10.10.10.251", "marca": "zte"}
            bloqueadas = await servicio._bloquear_zte(equipo, "aa:bb:cc:dd:ee:01", True, [1, 5])
            redes = acs.doc[IGD]["LANDevice"]["1"]["WLANConfiguration"]
            assert bloqueadas == {"aa:bb:cc:dd:ee:01"}
            # Las dos bandas van en la misma orden: nunca queda bloqueada una sola.
            cambios = [t for t in acs.tareas if t["name"] == "setParameterValues"]
            assert len(cambios) == 1 and {r.split("WLANConfiguration.")[1].split(".")[0]
                                          for r, _, _ in cambios[0]["parameterValues"]} == {"1", "5"}
            for i in ("1", "5"):
                assert redes[i]["X_TELMEX_COM_AccessControlMode"]["_value"] == "Ban"
                assert redes[i]["X_TELMEX_COM_AccessControl"]["1"]["MACAddress"]["_value"] == "aa:bb:cc:dd:ee:01"
            assert redes["2"]["X_TELMEX_COM_AccessControlMode"]["_value"] == "Disabled"
            # Otra vez no la duplica.
            await servicio._bloquear_zte(equipo, "aa:bb:cc:dd:ee:01", True, [1, 5])
            assert len(redes["1"]["X_TELMEX_COM_AccessControl"]) == 1
            assert await servicio._bloquear_zte(equipo, "aa:bb:cc:dd:ee:01", False, []) == set()
            assert redes["1"]["X_TELMEX_COM_AccessControl"] == {}
            assert redes["1"]["X_TELMEX_COM_AccessControlMode"]["_value"] == "Disabled"
        finally:
            httpx.AsyncClient = original

    asyncio.run(probar())
