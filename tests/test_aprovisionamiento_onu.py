"""La ONU del cliente nuevo se configura sola: PPPoE del cliente y WiFi con su contrato."""

import asyncio
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
                           creado_en=creado or datetime.now(), aplicado_en=None)


def genieacs(dispositivos, mac_epon=None):
    """GenieACS falso: `dispositivos` es {_id: serial}; la EPON nueva contesta su MAC al preguntarle."""
    tareas, preguntada = [], set()

    def manejar(request):
        if request.method == "POST" and request.url.path.endswith("/tasks"):
            tarea = json.loads(request.content)
            dispositivo = request.url.path.split("/")[2]
            tareas.append((dispositivo, tarea["name"], tarea))
            if tarea["name"] == "getParameterValues":
                preguntada.add(dispositivo)
            return httpx.Response(200, json={})
        if request.url.path == "/tasks":
            return httpx.Response(200, json=[])
        consulta = json.loads(request.url.params.get("query", "{}"))
        if "_deviceId._SerialNumber" in consulta:
            return httpx.Response(200, json=[{"_id": i} for i, s in dispositivos.items() if s == consulta["_deviceId._SerialNumber"]])
        if any(k.endswith("MACAddress._value") for k in consulta):
            ids = [i for i in preguntada if mac_epon and i == mac_epon[0]]
            if "_id" in consulta:
                ids = [i for i in ids if i == consulta["_id"]]
            return httpx.Response(200, json=[{"_id": i} for i in ids])
        if consulta.get("_id"):
            return httpx.Response(200, json=[{"_id": consulta["_id"], **DOCUMENTO}])
        # Lista para buscar EPON nuevas.
        ahora = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return httpx.Response(200, json=[{"_id": i, "_registered": ahora} for i in dispositivos])

    return httpx.MockTransport(manejar), tareas


def servicio(transporte, enviados):
    async def enviar(cliente, registro):
        enviados.append((cliente.telefono, registro and (registro.ssid, registro.clave_wifi)))

    return AprovisionamientoOnuService(BaseFalsa(CLIENTE), nbi="http://nbi", enviar_bienvenida=enviar, transporte=transporte)


def correr(srv, reg):
    async def probar():
        async with httpx.AsyncClient(base_url="http://nbi", transport=srv.transporte) as http:
            return await srv._procesar_uno(http, reg)

    return asyncio.run(probar())


def test_gpon_se_reconoce_por_serial_se_configura_y_se_le_manda_su_wifi():
    transporte, tareas = genieacs({"00259E-HG8145V5-48575443F1C8D69E": "48575443F1C8D69E"})
    enviados = []
    reg = registro("HWTCF1C8D69E")
    assert correr(servicio(transporte, enviados), reg) is True
    assert reg.estado == "aplicado" and reg.device_id == "00259E-HG8145V5-48575443F1C8D69E"
    cambio = next(t for _, nombre, t in tareas if nombre == "setParameterValues")
    assert [f"{WAN}.1.WANPPPConnection.1.Username", "juan_perez", "xsd:string"] in cambio["parameterValues"]
    # Su bienvenida sale ya con el WiFi puesto.
    assert enviados == [("9931112222", ("FdezNet-187B", "12345678"))]


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


def programar(identificador, modelo=None):
    onu = SimpleNamespace(identificador=identificador, modelo=modelo)
    db = ConsultasFalsas(SimpleNamespace(**vars(CLIENTE), onu_asignada=onu))
    resultado = asyncio.run(AprovisionamientoOnuService(db, nbi="http://nbi").programar(9))
    return resultado, db.agregados


def test_huawei_queda_esperando_para_configurarse_sola():
    resultado, agregados = programar("HWTCF1C8D69E", "HG8145V5")
    assert resultado["automatica"] is True and resultado["red"] == "FdezNet-187B"
    assert agregados[0].estado == "pendiente"


def test_zte_no_acepta_el_acs_y_el_tecnico_pone_el_wifi_a_mano():
    resultado, agregados = programar("ZTEG00000001", "F660")
    assert resultado["automatica"] is False and len(resultado["clave"]) == 8
    # No se espera a GenieACS: queda anotado para consultarlo en Mi red.
    assert agregados[0].estado == "manual"


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
