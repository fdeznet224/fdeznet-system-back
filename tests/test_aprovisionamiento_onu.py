"""La ONU del cliente nuevo se configura sola: PPPoE del cliente y WiFi con su contrato."""

import asyncio
import copy
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx

from src.application.services import aprovisionamiento_onu_service as aprov
from src.application.services.aprovisionamiento_onu_service import AprovisionamientoOnuService, valores_para

LAN = "InternetGatewayDevice.LANDevice.1"
WAN = "InternetGatewayDevice.WANDevice.1.WANConnectionDevice"


def v(valor):
    return {"_value": valor}


DOCUMENTO = {"InternetGatewayDevice": {
    "LANDevice": {"1": {"WLANConfiguration": {
        "1": {"SSID": v("HUAWEI-2.4G-ABCD"), "X_HW_RFBand": v("2.4GHz")},
        "2": {"SSID": v("Zona FdezNet"), "X_HW_RFBand": v("2.4GHz")},
        "5": {"SSID": v("HUAWEI-2.4G-ABCD"), "X_HW_RFBand": v("5GHz")},
    }}},
    "WANDevice": {"1": {"WANConnectionDevice": {
        "1": {"WANPPPConnection": {"1": {"X_HW_SERVICELIST": v("TR069_INTERNET"), "Username": v("FdezNet")}}},
        "2": {"WANIPConnection": {"1": {"X_HW_SERVICELIST": v("INTERNET")}}},
    }}},
}}


def test_pone_el_wifi_en_las_dos_bandas_de_su_red_y_el_pppoe_al_final():
    nombres, indices, pppoe = valores_para(DOCUMENTO, "FdezNet-187B", "juan_perez", "clave-pppoe")
    assert [r for r, _, _ in nombres] == [f"{LAN}.WLANConfiguration.1.SSID", f"{LAN}.WLANConfiguration.5.SSID"]
    # La red de fichas no se toca.
    assert indices == [1, 5]
    assert pppoe == [[f"{WAN}.1.WANPPPConnection.1.Username", "juan_perez", "xsd:string"],
                     [f"{WAN}.1.WANPPPConnection.1.Password", "clave-pppoe", "xsd:string"]]


def test_nombre_y_contrasena_del_wifi():
    assert aprov.ssid_para("187B") == "FdezNet-187B"
    clave = aprov.clave_wifi_nueva()
    assert len(clave) == 8 and clave.isdigit()


class BaseFalsa:
    def __init__(self, cliente):
        self.cliente = cliente

    async def get(self, modelo, llave):
        return self.cliente

    async def commit(self):
        pass


CLIENTE = SimpleNamespace(id=9, nombre="Juan Pérez", cedula="187B", telefono="9931112222",
                          user_pppoe="juan_perez", pass_pppoe="clave-pppoe")


def registro(identificador, creado=None):
    return SimpleNamespace(cliente_id=9, identificador=identificador, ssid="FdezNet-187B", clave_wifi="12345678",
                           estado="pendiente", intentos=0, detalle=None, device_id=None,
                           creado_en=creado or datetime.now(), aplicado_en=None, cambio_onu=False, wifi_nuevo=False)


def genieacs(dispositivos, mac_epon=None, documento=None, corta_pppoe=False):
    """GenieACS falso: `dispositivos` es {_id: serial}; la EPON nueva contesta su MAC al preguntarle.
    `corta_pppoe`: como la ZTE, aplica el PPPoE pero corta la sesión sin contestar."""
    tareas, preguntada = [], set()
    documento = documento if documento is not None else DOCUMENTO

    def manejar(request):
        if request.method == "POST" and request.url.path.endswith("/tasks"):
            tarea = json.loads(request.content)
            dispositivo = request.url.path.split("/")[2]
            tareas.append((dispositivo, tarea["name"], tarea))
            if tarea["name"] == "getParameterValues":
                preguntada.add(dispositivo)
            if tarea["name"] == "setParameterValues":
                for ruta, valor, _ in tarea["parameterValues"]:
                    nodo = documento
                    *camino, campo = ruta.split(".")
                    for parte in camino:
                        nodo = nodo.setdefault(parte, {})
                    nodo[campo] = {"_value": valor}
                if corta_pppoe and any(r.endswith(".Username") for r, _, _ in tarea["parameterValues"]):
                    return httpx.Response(202, json={"_id": "t1"})
            return httpx.Response(200, json={})
        if request.url.path == "/faults":
            return httpx.Response(200, json=[{"_id": "dev:task_t1", "channel": "task_t1", "code": "session_terminated"}])
        if request.method == "DELETE":
            return httpx.Response(200, json={})
        if request.url.path == "/tasks":
            return httpx.Response(200, json=[])
        consulta = json.loads(request.url.params.get("query", "{}"))
        if "_deviceId._SerialNumber" in consulta:
            buscados = consulta["_deviceId._SerialNumber"]
            buscados = buscados["$in"] if isinstance(buscados, dict) else [buscados]
            return httpx.Response(200, json=[{"_id": i} for i, s in dispositivos.items() if s in buscados])
        if any(k.endswith("MACAddress._value") for k in consulta):
            ids = [i for i in preguntada if mac_epon and i == mac_epon[0]]
            if "_id" in consulta:
                ids = [i for i in ids if i == consulta["_id"]]
            return httpx.Response(200, json=[{"_id": i} for i in ids])
        if consulta.get("_id"):
            return httpx.Response(200, json=[{"_id": consulta["_id"], **documento}])
        # Lista para buscar EPON nuevas.
        ahora = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return httpx.Response(200, json=[{"_id": i, "_registered": ahora} for i in dispositivos])

    return httpx.MockTransport(manejar), tareas


def servicio(transporte, enviados, avisos=None):
    async def enviar(cliente, registro):
        enviados.append((cliente.telefono, registro and (registro.ssid, registro.clave_wifi)))

    async def wifi_nuevo(cliente, registro):
        (avisos if avisos is not None else []).append((cliente.telefono, registro.ssid, registro.clave_wifi))

    return AprovisionamientoOnuService(BaseFalsa(CLIENTE), nbi="http://nbi", enviar_bienvenida=enviar, transporte=transporte,
                                       enviar_wifi_nuevo=wifi_nuevo)


def correr(srv, reg):
    async def probar():
        async with httpx.AsyncClient(base_url="http://nbi", transport=srv.transporte) as http:
            return await srv._procesar_uno(http, reg)

    return asyncio.run(probar())


def test_gpon_se_reconoce_por_serial_se_configura_y_se_le_manda_su_wifi():
    transporte, tareas = genieacs({"00259E-HG8145V5-48575443F1C8D69E": "48575443F1C8D69E"}, documento=copy.deepcopy(DOCUMENTO))
    enviados = []
    reg = registro("HWTCF1C8D69E")
    assert correr(servicio(transporte, enviados), reg) is True
    assert reg.estado == "aplicado" and reg.device_id == "00259E-HG8145V5-48575443F1C8D69E"
    wifi, pppoe = [t for _, nombre, t in tareas if nombre == "setParameterValues"]
    # El WiFi primero y el PPPoE aparte, al final.
    assert all(".WLANConfiguration." in r for r, _, _ in wifi["parameterValues"])
    assert [f"{WAN}.1.WANPPPConnection.1.Username", "juan_perez", "xsd:string"] in pppoe["parameterValues"]
    # Su bienvenida sale ya con el WiFi puesto.
    assert enviados == [("9931112222", ("FdezNet-187B", "12345678"))]


def test_onu_de_reemplazo_se_configura_sin_mandar_bienvenida():
    transporte, tareas = genieacs({"00259E-HG8145V5-48575443F1C8D69E": "48575443F1C8D69E"})
    enviados = []
    reg = registro("HWTCF1C8D69E")
    reg.cambio_onu = True
    assert correr(servicio(transporte, enviados), reg) is True
    assert reg.estado == "aplicado" and enviados == []
    assert any(nombre == "setParameterValues" for _, nombre, _ in tareas)


def test_si_aun_no_se_reporta_se_sigue_esperando():
    transporte, tareas = genieacs({})
    reg = registro("HWTCF1C8D69E")
    assert correr(servicio(transporte, []), reg) is False
    assert reg.estado == "pendiente" and tareas == []


def test_epon_nueva_se_reconoce_por_su_mac():
    transporte, tareas = genieacs({"00259E-HG8145X6-A": "48575443AEB7B6AD", "00259E-HG8145X6-B": "485754430000BBBB"},
                                  mac_epon=("00259E-HG8145X6-A",))
    reg = registro("3C:FF:D8:E7:4D:EB")
    assert correr(servicio(transporte, []), reg) is True
    assert reg.device_id == "00259E-HG8145X6-A"


def test_a_las_24_horas_deja_de_esperar():
    transporte, _ = genieacs({})
    reg = registro("HWTCF1C8D69E", creado=datetime.now() - timedelta(hours=25))
    enviados = []
    correr(servicio(transporte, enviados), reg)
    assert reg.estado == "vencido" and "ACS" in reg.detalle
    # La bienvenida sale igual, sin un WiFi que no se pudo poner.
    assert enviados == [("9931112222", None)]


class ConsultasFalsas:
    def __init__(self, cliente):
        self.cliente, self.agregados = cliente, []

    async def execute(self, _consulta):
        objetos = [self.cliente] if not self.agregados and not getattr(self, "_leido", False) else []
        self._leido = True
        return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: objetos[0] if objetos else None, all=lambda: objetos[1:]))

    def add(self, objeto):
        self.agregados.append(objeto)

    async def delete(self, objeto):
        pass

    async def commit(self):
        pass


def programar_con(identificador, modelo=None, en_el_acs=(), **opciones):
    onu = SimpleNamespace(identificador=identificador, modelo=modelo)
    db = ConsultasFalsas(SimpleNamespace(**vars(CLIENTE), onu_asignada=onu))
    transporte, _ = genieacs({f"44F436-F670L-{s}": s for s in en_el_acs})
    resultado = asyncio.run(AprovisionamientoOnuService(db, nbi="http://nbi", transporte=transporte).programar(9, **opciones))
    return resultado, db.agregados


def programar(identificador, modelo=None):
    return programar_con(identificador, modelo)


def test_huawei_queda_esperando_para_configurarse_sola():
    resultado, agregados = programar("HWTCF1C8D69E", "HG8145V5")
    assert resultado["automatica"] is True and resultado["red"] == "FdezNet-187B"
    assert agregados[0].estado == "pendiente"


def test_zte_que_no_entro_al_acs_la_configura_el_tecnico_a_mano():
    resultado, agregados = programar("ZTEG00000001", "F660")
    assert resultado["automatica"] is False and len(resultado["clave"]) == 8
    # No se espera a GenieACS: queda anotado para consultarlo en Mi red.
    assert agregados[0].estado == "manual"


def test_zte_que_ya_esta_en_el_acs_se_configura_sola():
    resultado, agregados = programar_con("ZTEG24284C96", "F670L", en_el_acs=("ZTEG24284C96",))
    assert resultado["automatica"] is True and agregados[0].estado == "pendiente"


ZTE = {"InternetGatewayDevice": {
    "LANDevice": {"1": {"WLANConfiguration": {
        "1": {"SSID": v("INFINITUM6156_2.4"), "Enable": v(True), "X_ZTE-COM_OperatingFrequencyBand": v("2.4GHz")},
        "2": {"SSID": v("Zona FdezNet"), "Enable": v(True), "X_ZTE-COM_OperatingFrequencyBand": v("2.4GHz")},
        "5": {"SSID": v("INFINITUM6156_5"), "Enable": v(True), "X_ZTE-COM_OperatingFrequencyBand": v("5GHz")},
        "6": {"SSID": v("Zona FdezNet"), "Enable": v(True), "X_ZTE-COM_OperatingFrequencyBand": v("5GHz")},
        "7": {"SSID": v("SSID7"), "Enable": v(False), "X_ZTE-COM_OperatingFrequencyBand": v("5GHz")},
    }}},
    "WANDevice": {"1": {"WANConnectionDevice": {"1": {
        "WANPPPConnection": {"2": {"X_ZTE-COM_ServiceList": v("INTERNET_TR069"), "ConnectionType": v("IP_Routed")}},
        "WANIPConnection": {"1": {"X_ZTE-COM_ServiceList": v("INTERNET"), "ConnectionType": v("IP_Bridged")}},
    }}}},
}}


def test_zte_renombra_sus_dos_bandas_aunque_se_llamen_distinto_y_no_la_de_fichas():
    nombres, indices, pppoe = valores_para(ZTE, "FdezNet-187B", "juan_perez", "clave-pppoe")
    assert indices == [1, 5]
    assert pppoe[0][0] == f"{WAN}.1.WANPPPConnection.2.Username"


def test_onu_de_reemplazo_lleva_el_wifi_que_ya_tenia():
    resultado, agregados = programar_con("HWTC22222222", ssid="Casa Lopez", clave="lopez2024", cambio_onu=True)
    assert resultado == {"red": "Casa Lopez", "clave": "lopez2024", "automatica": True, "clave_nueva": False, "aviso": True}
    assert agregados[0].cambio_onu is True and agregados[0].estado == "pendiente"


def test_si_no_se_sabe_su_wifi_va_uno_nuevo():
    resultado, _ = programar_con("HWTC22222222", cambio_onu=True)
    assert resultado["red"] == "FdezNet-187B" and resultado["clave_nueva"] is True


def test_si_la_onu_rechaza_un_campo_de_la_contrasena_prueba_el_otro():
    """La HG8145V5 V5R020 contesta 9007 a KeyPassphrase; la V5R019 acepta los dos."""
    enviados = []

    async def tarea(http, device_id, tarea):
        campos = [r for r, _, _ in tarea["parameterValues"]]
        enviados.append(campos)
        if any(c.endswith(".PreSharedKey.1.KeyPassphrase") for c in campos):
            raise ValueError("La ONU rechazó el cambio: Invalid parameter value")
        return True

    srv = AprovisionamientoOnuService(BaseFalsa(CLIENTE), nbi="http://nbi")
    srv.red._tarea = tarea
    nombres = [[f"{LAN}.WLANConfiguration.1.SSID", "FdezNet-187B", "xsd:string"]]
    assert asyncio.run(srv.red.poner_con_clave_wifi(None, "dev", [1, 5], "12345678", nombres)) is True
    assert enviados[1] == [f"{LAN}.WLANConfiguration.1.SSID", f"{LAN}.WLANConfiguration.1.KeyPassphrase",
                           f"{LAN}.WLANConfiguration.5.KeyPassphrase"]


def test_la_bienvenida_quita_los_renglones_del_wifi_si_no_hay():
    from src.application.services.notification_service import PLANTILLAS_OBLIGATORIAS, sin_lineas_vacias

    texto = PLANTILLAS_OBLIGATORIAS["bienvenida"]
    sin_wifi = sin_lineas_vacias(texto, {"red_wifi": "", "clave_wifi": "", "portal": "https://x/cliente"})
    assert "{red_wifi}" not in sin_wifi and "{clave_wifi}" not in sin_wifi and "{portal}" in sin_wifi
    assert "\n\n\n" not in sin_wifi
    assert sin_lineas_vacias(texto, {"red_wifi": "R", "clave_wifi": "C", "portal": "P"}) == texto


def test_toma_las_dos_bandas_aunque_ya_no_tengan_el_mismo_nombre():
    import copy

    documento = copy.deepcopy(DOCUMENTO)
    wlan = documento["InternetGatewayDevice"]["LANDevice"]["1"]["WLANConfiguration"]
    wlan["1"]["SSID"] = v("FdezNet-187B")
    wlan["5"]["SSID"] = v("FdezNet-")
    _, indices, _ = valores_para(documento, "FdezNet-187B", "juan_perez", "clave-pppoe")
    assert indices == [1, 5]


def test_el_wifi_conocido_sale_de_lo_que_puso_el_sistema_o_el_cliente():
    class UnRegistro:
        async def execute(self, _consulta):
            registro = SimpleNamespace(ssid="FdezNet-187B", clave_wifi="11223344")
            return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: registro))

    srv = AprovisionamientoOnuService(UnRegistro(), nbi="http://nbi")
    assert asyncio.run(srv.wifi_conocido(9)) == {"red": "FdezNet-187B", "clave": "11223344"}


def test_la_zte_se_reconoce_por_su_serial_tal_cual():
    transporte, _ = genieacs({"44F436-F670L-ZTEG24284C96": "ZTEG24284C96"})
    reg = registro("ZTEG24284C96")
    assert correr(servicio(transporte, []), reg) is True
    assert reg.device_id == "44F436-F670L-ZTEG24284C96"


def test_si_la_zte_corta_al_cambiar_el_pppoe_no_se_da_por_hecho_hasta_verlo_puesto():
    """Sindy, 7 nov: se marcó "aplicado" sin confirmar y la ONU no lo tenía."""
    documento = copy.deepcopy(ZTE)
    transporte, tareas = genieacs({"44F436-F670L-ZTEG24146156": "ZTEG24146156"}, documento=documento, corta_pppoe=True)
    reg = registro("ZTEG24146156")
    srv = servicio(transporte, [])
    assert correr(srv, reg) is False
    assert reg.estado == "pendiente" and "esperando" in reg.detalle and reg.device_id
    # En la siguiente vuelta ya se ve su red y su PPPoE en la ONU: ahora sí quedó, sin volver a mandarlo.
    enviados_antes = len([t for t in tareas if t[1] == "setParameterValues"])
    assert correr(srv, reg) is True
    assert reg.estado == "aplicado" and reg.detalle is None
    assert len([t for t in tareas if t[1] == "setParameterValues"]) == enviados_antes


def test_si_no_quedo_se_vuelve_a_mandar_y_al_final_se_da_por_fallido():
    documento = copy.deepcopy(ZTE)
    transporte, tareas = genieacs({"44F436-F670L-ZTEG24146156": "ZTEG24146156"}, documento=documento, corta_pppoe=True)
    reg = registro("ZTEG24146156")
    srv = servicio(transporte, [])
    correr(srv, reg)
    # La ONU lo descartó: no tiene ni la red ni el PPPoE.
    wlan = documento["InternetGatewayDevice"]["LANDevice"]["1"]["WLANConfiguration"]
    wlan["1"]["SSID"] = v("INFINITUM6156_2.4")
    for _ in range(aprov.MAX_SIN_CONFIRMAR - 1):
        wlan["1"]["SSID"] = v("INFINITUM6156_2.4")
        correr(srv, reg)
    assert reg.estado == "error" and "revisa la ONU" in reg.detalle
    assert len([t for t in tareas if t[1] == "setParameterValues"]) > 2


def test_cambio_de_onu_con_wifi_nuevo_se_le_manda_a_la_clienta_al_quedar():
    transporte, _ = genieacs({"00259E-HG8145V5-48575443F1C8D69E": "48575443F1C8D69E"}, documento=copy.deepcopy(DOCUMENTO))
    avisos, enviados = [], []
    reg = registro("HWTCF1C8D69E")
    reg.cambio_onu, reg.wifi_nuevo = True, True
    assert correr(servicio(transporte, enviados, avisos), reg) is True
    assert enviados == [] and avisos == [("9931112222", "FdezNet-187B", "12345678")]


def test_cambio_de_onu_con_el_mismo_wifi_no_manda_mensaje():
    transporte, _ = genieacs({"00259E-HG8145V5-48575443F1C8D69E": "48575443F1C8D69E"}, documento=copy.deepcopy(DOCUMENTO))
    avisos = []
    reg = registro("HWTCF1C8D69E")
    reg.cambio_onu = True
    assert correr(servicio(transporte, [], avisos), reg) is True
    assert avisos == []


def test_al_programar_un_cambio_de_onu_sabe_si_el_wifi_es_nuevo():
    mismo, agregados = programar_con("HWTC22222222", ssid="Casa Lopez", clave="lopez2024", cambio_onu=True,
                                     anterior={"red": "Casa Lopez", "clave": "lopez2024"})
    assert mismo["aviso"] is False and agregados[0].wifi_nuevo is False
    # Venía de antena: no se sabía su WiFi, va FdezNet-CONTRATO con contraseña nueva y se le avisa.
    nuevo, agregados = programar_con("HWTC22222222", cambio_onu=True, anterior={"red": None, "clave": None})
    assert nuevo["red"] == "FdezNet-187B" and nuevo["aviso"] is True and agregados[0].wifi_nuevo is True
    # Se sabía el nombre pero no la contraseña (la Huawei no la deja leer): contraseña nueva, se avisa.
    solo_nombre, _ = programar_con("HWTC22222222", ssid="Casa Lopez", cambio_onu=True, anterior={"red": "Casa Lopez", "clave": None})
    assert solo_nombre["aviso"] is True


def test_si_no_se_pudo_sola_el_estado_trae_el_pppoe_para_ponerlo_a_mano():
    class Base:
        def __init__(self, estado):
            self.reg = SimpleNamespace(estado=estado, ssid="FdezNet-3DCB", clave_wifi="12345678", detalle=None, cambio_onu=True,
                                       wifi_nuevo=True, intentos=5, creado_en=datetime.now(), aplicado_en=None)

        async def execute(self, _consulta):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: self.reg))

        async def get(self, _modelo, _id):
            return CLIENTE

    fallo = asyncio.run(AprovisionamientoOnuService(Base("error"), nbi="http://nbi").del_cliente(9))
    assert fallo["usuario_pppoe"] == "juan_perez" and fallo["clave_pppoe"] == "clave-pppoe"
    listo = asyncio.run(AprovisionamientoOnuService(Base("aplicado"), nbi="http://nbi").del_cliente(9))
    assert "usuario_pppoe" not in listo
