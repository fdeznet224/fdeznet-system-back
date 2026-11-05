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
        "Firewall": {f"{prefijo}_MacFilterService": {"Enable": v(False), "Mode": v("Black List"), "Filter": {}}},
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
                              else {"Name": v("FWMACF1"), "Type": v("Bridge"), "Protocol": v("IP"),
                                    "SourceMACAddress": v("00:00:00:00:00:00")} if "MacFilter" in tarea["objectName"]
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

    def mac_de_filtro_invalida(self, tarea):
        """La F670L rechaza la MAC de una regla del firewall que sigue en Bridge o que ya está en otra."""
        for ruta, valor, _ in tarea.get("parameterValues") or []:
            if "MacFilterService.Filter." in ruta and ruta.endswith(".SourceMACAddress"):
                filtro, _, _ = ruta.rpartition(".")
                reglas, _, n = filtro.rpartition(".")
                if self._nodo(filtro)["Type"]["_value"] != "Routing":
                    return True
                if any(k != n and r["SourceMACAddress"]["_value"] == valor for k, r in self._nodo(reglas).items()):
                    return True
        return False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/tasks"):
            tarea = json.loads(request.content)
            self.tareas.append(tarea)
            if self.rechaza(tarea) or self.mac_de_filtro_invalida(tarea):
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


def con_acs_falso(acs, prueba):
    """Corre `prueba(servicio)` con el RedClienteService hablando con el GenieACS falso."""
    servicio = RedClienteService(None, nbi="http://nbi")
    original = httpx.AsyncClient

    async def probar():
        async def id_tr069(_http, _equipo):
            return "dev"

        servicio._id_tr069 = id_tr069
        httpx.AsyncClient = lambda **kw: original(**{**kw, "transport": httpx.MockTransport(acs)})
        try:
            return await prueba(servicio)
        finally:
            httpx.AsyncClient = original

    return asyncio.run(probar())


EQUIPO = {"identificador": "ZTEG25520366", "ip": "10.10.10.251", "marca": "zte"}
SAMSUNG = "be:d6:86:40:9f:c2"


def con_host(documento, mac, wlan):
    documento[IGD]["LANDevice"]["1"]["Hosts"] = {"Host": {"1": {
        "MACAddress": v(mac), "HostName": v("Galaxy-A56-5G"), "Active": v(True), "InterfaceType": v("802.11"),
        "Layer2Interface": v(f"{IGD}.LANDevice.1.WLANConfiguration.{wlan}")}}}
    return documento


def test_bloquea_en_el_filtro_del_firewall_y_no_en_la_lista_del_wifi():
    # La lista del WiFi rechaza al Samsung al conectarse y estrena otra MAC; el filtro del
    # firewall lo deja conectado con su misma MAC y sin internet.
    acs = AcsFalso(con_host(zte(), SAMSUNG, 1))
    filtro = acs.doc[IGD]["Firewall"]["X_TELMEX_COM_MacFilterService"]
    redes = acs.doc[IGD]["LANDevice"]["1"]["WLANConfiguration"]

    async def prueba(servicio):
        bloqueadas, por_wifi = await servicio._bloquear_zte(EQUIPO, SAMSUNG, True, solo_principal=True)
        assert bloqueadas == {SAMSUNG} and por_wifi == []
        regla = filtro["Filter"]["1"]
        assert {k: regla[k]["_value"] for k in ("Type", "Protocol", "SourceMACAddress")} == {
            "Type": "Routing", "Protocol": "Any", "SourceMACAddress": SAMSUNG}
        assert filtro["Enable"]["_value"] is True and filtro["Mode"]["_value"] == "Black List"
        assert all(redes[i]["X_TELMEX_COM_AccessControlMode"]["_value"] == "Disabled" for i in redes)
        # Otra vez no la duplica (la ZTE rechazaría la MAC repetida).
        await servicio._bloquear_zte(EQUIPO, SAMSUNG, True)
        assert len(filtro["Filter"]) == 1
        assert (await servicio._bloquear_zte(EQUIPO, SAMSUNG, False))[0] == set()
        assert filtro["Filter"] == {}

    con_acs_falso(acs, prueba)


def test_la_regla_que_no_acepta_la_mac_no_se_queda():
    acs = AcsFalso(con_host(zte(), SAMSUNG, 1),
                   lambda t: any(r.endswith("SourceMACAddress") for r, _, _ in t.get("parameterValues") or []))
    filtro = acs.doc[IGD]["Firewall"]["X_TELMEX_COM_MacFilterService"]

    async def prueba(servicio):
        try:
            await servicio._bloquear_zte(EQUIPO, SAMSUNG, True)
        except ValueError:
            return
        raise AssertionError("debió avisar que la ONU rechazó el cambio")

    con_acs_falso(acs, prueba)
    assert filtro["Filter"] == {} and filtro["Enable"]["_value"] is False


def test_no_cambia_un_filtro_en_lista_blanca():
    documento = con_host(zte(), SAMSUNG, 1)
    documento[IGD]["Firewall"]["X_TELMEX_COM_MacFilterService"].update(Enable=v(True), Mode=v("White List"))
    acs = AcsFalso(documento)

    async def prueba(servicio):
        try:
            await servicio._bloquear_zte(EQUIPO, SAMSUNG, True)
        except ValueError as error:
            assert "lista blanca" in str(error)
            return
        raise AssertionError("no debió tocar la lista blanca")

    con_acs_falso(acs, prueba)
    assert acs.doc[IGD]["Firewall"]["X_TELMEX_COM_MacFilterService"]["Filter"] == {}


def test_equipo_de_la_red_de_fichas_va_en_la_lista_del_wifi_de_las_dos_bandas():
    # La red de fichas va en puente: el firewall no la ve. Las dos bandas en una sola orden.
    acs = AcsFalso(con_host(zte(), SAMSUNG, 2))
    redes = acs.doc[IGD]["LANDevice"]["1"]["WLANConfiguration"]

    async def prueba(servicio):
        bloqueadas, por_wifi = await servicio._bloquear_zte(EQUIPO, SAMSUNG, True)
        assert bloqueadas == {SAMSUNG} and por_wifi == [2, 6]
        cambios = [t for t in acs.tareas if t["name"] == "setParameterValues"]
        assert len(cambios) == 1
        for i in ("2", "6"):
            assert redes[i]["X_TELMEX_COM_AccessControlMode"]["_value"] == "Ban"
        assert acs.doc[IGD]["Firewall"]["X_TELMEX_COM_MacFilterService"]["Filter"] == {}
        assert (await servicio._bloquear_zte(EQUIPO, SAMSUNG, False))[0] == set()
        assert redes["2"]["X_TELMEX_COM_AccessControlMode"]["_value"] == "Disabled"

    con_acs_falso(acs, prueba)


def test_bloquear_en_el_firewall_quita_el_bloqueo_de_antes_en_el_wifi():
    documento = con_host(zte(), SAMSUNG, 1)
    for i in ("1", "5"):
        documento[IGD]["LANDevice"]["1"]["WLANConfiguration"][i].update(
            X_TELMEX_COM_AccessControlMode=v("Ban"), X_TELMEX_COM_AccessControl={"1": {"MACAddress": v(SAMSUNG)}})
    acs = AcsFalso(documento)
    redes = acs.doc[IGD]["LANDevice"]["1"]["WLANConfiguration"]

    async def prueba(servicio):
        assert (await servicio._bloquear_zte(EQUIPO, SAMSUNG, True))[0] == {SAMSUNG}

    con_acs_falso(acs, prueba)
    for i in ("1", "5"):
        assert redes[i]["X_TELMEX_COM_AccessControl"] == {}
        assert redes[i]["X_TELMEX_COM_AccessControlMode"]["_value"] == "Disabled"


def test_bloqueados_salen_del_filtro_del_firewall_encendido():
    documento = zte()
    filtro = documento[IGD]["Firewall"]["X_TELMEX_COM_MacFilterService"]
    filtro["Filter"] = {"1": {"SourceMACAddress": v(SAMSUNG), "Type": v("Routing")}}
    lan = documento[IGD]["LANDevice"]["1"]
    assert bloqueadas_zte(lan, documento[IGD]["Firewall"]) == set()  # Apagado no bloquea.
    filtro["Enable"] = v(True)
    assert bloqueadas_zte(lan, documento[IGD]["Firewall"]) == {SAMSUNG}


def test_bloquear_en_el_firewall_son_cuatro_ordenes_sin_releer_el_wifi():
    # La app espera 25 s; releer el WiFi de la ZTE tarda ~12 s. Bloquear: leer el filtro,
    # crear la regla, tipo/protocolo/nombre, MAC con el filtro encendido.
    acs = AcsFalso(con_host(zte(), SAMSUNG, 1))

    async def prueba(servicio):
        await servicio._bloquear_zte(EQUIPO, SAMSUNG, True)

    con_acs_falso(acs, prueba)
    assert [t["name"] for t in acs.tareas] == ["refreshObject", "addObject", "setParameterValues", "setParameterValues"]
    assert acs.tareas[0]["objectName"].endswith("X_TELMEX_COM_MacFilterService")


def test_la_confirmacion_del_portal_espera_al_bloqueo_en_curso():
    # Dos sesiones a la vez con la ZTE: no contesta la segunda y GenieACS la da por desconectada.
    acs = AcsFalso(con_host(zte(), SAMSUNG, 1))
    en_curso, maximo = 0, 0

    async def lenta(request):
        nonlocal en_curso, maximo
        if request.method == "POST":
            en_curso += 1
            maximo = max(maximo, en_curso)
            await asyncio.sleep(0.01)
            en_curso -= 1
        return acs(request)

    servicio = RedClienteService(None, nbi="http://nbi")
    original = httpx.AsyncClient

    async def probar():
        async def id_tr069(_http, _equipo):
            return "dev"

        servicio._id_tr069 = id_tr069
        httpx.AsyncClient = lambda **kw: original(**{**kw, "transport": httpx.MockTransport(lenta)})
        try:
            return await asyncio.gather(servicio._bloquear_zte(EQUIPO, SAMSUNG, True), servicio._bloqueadas_zte(EQUIPO))
        finally:
            httpx.AsyncClient = original

    (bloqueadas, _), confirmadas = asyncio.run(probar())
    assert maximo == 1
    assert bloqueadas == confirmadas == {SAMSUNG}


def test_modelo_del_modem_para_el_portal():
    from src.application.services.red_cliente_service import modelo_tr069
    documento = {IGD: {"DeviceInfo": {"ModelName": v("F670L")}}}
    assert modelo_tr069(documento, "zte") == "ZTE F670L"
    assert modelo_tr069({IGD: {"DeviceInfo": {"ModelName": v("HG8145V5")}}}, "huawei") == "Huawei HG8145V5"
    assert modelo_tr069({IGD: {"DeviceInfo": {"ModelName": v("ZTE F670L")}}}, "zte") == "ZTE F670L"
    assert modelo_tr069({}, "zte") is None


def test_el_relleno_del_alta_no_es_modelo_y_se_cambia_por_el_que_reporta_la_onu():
    # El técnico da de alta la ONU sin modelo: queda "Registrada por el técnico" y el portal
    # lo mostraba como nombre del módem.
    from types import SimpleNamespace
    from src.application.services.acceso_onu_config import modelo_real

    assert modelo_real("Registrada por el técnico") is None and modelo_real(" ") is None
    assert modelo_real("ZTE F670L") == "ZTE F670L"

    onu = SimpleNamespace(modelo="Registrada por el técnico")
    equipo = {"marca": "zte", "modelo": modelo_real(onu.modelo), "_onu": onu}
    documento = {"_deviceId": {"_Manufacturer": "ZTE", "_ProductClass": "F670L"}}

    class Db:
        async def commit(self):
            pass

    async def probar():
        transporte = httpx.MockTransport(lambda r: httpx.Response(200, json=[documento]))
        async with httpx.AsyncClient(base_url="http://nbi", transport=transporte) as http:
            await RedClienteService(Db(), nbi="http://nbi")._marca_por_tr069(http, "dev", equipo)

    asyncio.run(probar())
    assert onu.modelo == equipo["modelo"] == "ZTE F670L"
