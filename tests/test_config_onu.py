"""Acceso remoto por TR-069: WAN, enlaces, WiFi, LAN y puertos, con ONU como las reales (7 nov 2026)."""

import asyncio
import copy

import httpx
import pytest

from src.application.services import config_onu_service as cfg
from src.application.services.config_onu_service import ConfigOnuService
from test_plantilla_zte import AcsFalso, v, zte

IGD = "InternetGatewayDevice"
WCD = f"{IGD}.WANDevice.1.WANConnectionDevice"


def huawei():
    """La HG8145V5 del dueño: PPPoE TR069_INTERNET VLAN 10 (LAN1-3, SSID1/5) y puente VLAN 4 (LAN4, SSID2/3)."""
    def bind(lans, ssids):
        return {**{f"Lan{i}Enable": v(1 if i in lans else 0) for i in range(1, 5)},
                **{f"SSID{i}Enable": v(1 if i in ssids else 0) for i in range(1, 5)}}

    def wlan(ssid, estandar, ht, beacon="WPAand11i"):
        return {"SSID": v(ssid), "Enable": v(True), "SSIDAdvertisementEnabled": v(True), "AutoChannelEnable": v(True),
                "Channel": v(7 if estandar == "11bgn" else 149), "ChannelsInUse": v("7"), "Standard": v(estandar),
                "X_HW_Standard": v(estandar), "X_HW_HT20": v(ht), "BeaconType": v(beacon)}

    return {
        "_lastInform": "2026-11-07T17:35:23.941Z",
        IGD: {
            "ManagementServer": {"ConnectionRequestURL": v("http://10.10.10.254:7547/")},
            "WANDevice": {"1": {"WANConnectionDevice": {
                "1": {"WANPPPConnection": {"1": {
                    "Name": v("1_TR069_INTERNET_R_VID_10"), "ConnectionType": v("IP_Routed"), "Enable": v(True),
                    "ConnectionStatus": v("Connected"), "ExternalIPAddress": v("10.10.10.254"), "Username": v("Casa"),
                    "NATEnabled": v(True), "DNSServers": v("10.10.10.1"), "X_HW_VLAN": v(10),
                    "X_HW_SERVICELIST": v("TR069_INTERNET"), "X_HW_LANBIND": bind({1, 2, 3}, {1}),
                    "X_HW_DMZ": {"DMZEnable": v(False), "DMZHostIPAddress": v("")}, "PortMapping": {},
                }}},
                "2": {"WANIPConnection": {"1": {
                    "Name": v("2_INTERNET_B_VID_4"), "ConnectionType": v("IP_Bridged"), "Enable": v(True),
                    "AddressingType": v("DHCP"), "ConnectionStatus": v("Connected"), "NATEnabled": v(False),
                    "X_HW_VLAN": v(4), "X_HW_SERVICELIST": v("INTERNET"), "X_HW_LANBIND": bind({4}, {2, 3}),
                }}},
            }}},
            "LANDevice": {"1": {
                "LANEthernetInterfaceConfig": {str(i): {"Enable": v(True)} for i in range(1, 5)},
                "WLANConfiguration": {"1": wlan("FdezNet-Central-VG", "11bgn", 0), "2": wlan("Zona FdezNet", "11bgn", 0, "Basic"),
                                      "3": wlan("Zona FdezNet", "11ac", 3, "Basic"), "5": wlan("FdezNet-Central-VG", "11ac", 3)},
                "LANHostConfigManagement": {
                    "DHCPServerEnable": v(True), "MinAddress": v("192.168.100.2"), "MaxAddress": v("192.168.100.254"),
                    "DHCPLeaseTime": v(86400), "DNSServers": v("192.168.100.1"), "SubnetMask": v("255.255.255.0"),
                    "IPRouters": v("192.168.100.1"),
                    "IPInterface": {"1": {"Enable": v(True), "IPInterfaceIPAddress": v("192.168.100.1"),
                                          "IPInterfaceSubnetMask": v("255.255.255.0")}},
                    "DHCPStaticAddress": {"1": {"Chaddr": v("C0:74:AD:4C:42:CC"), "Yiaddr": v("192.168.22.100")}},
                },
            }},
            "Layer3Forwarding": {"X_HW_policy_route": {
                "1": {"PolicyRouteType": v("SourcePhyPort"), "WanName": v("wan1.1.ppp1"), "PhyPortName": v("LAN1,LAN2,LAN3,SSID5,SSID1")},
                "2": {"PolicyRouteType": v("SourcePhyPort"), "WanName": v("wan1.2.ip1"), "PhyPortName": v("LAN4,SSID3,SSID2")},
            }},
        },
    }


def zte_completa():
    """La F670L de prueba con su WAN de fichas y los dos enlaces, como quedó el 4 nov."""
    doc = zte()
    p = "X_TELMEX_COM"
    igd = doc[IGD]
    igd["WANDevice"]["1"]["WANConnectionDevice"]["1"]["WANIPConnection"]["1"] = {
        "Name": v("Zona FdezNet"), "ConnectionType": v("IP_Bridged"), "Enable": v(True),
        f"{p}_VLANEnable": v(True), f"{p}_VLANID": v(4), f"{p}_ServiceList": v("INTERNET"),
    }
    ppp = igd["WANDevice"]["1"]["WANConnectionDevice"]["1"]["WANPPPConnection"]["2"]
    ppp.update(Enable=v(True), ExternalIPAddress=v("10.10.10.250"), **{f"{p}_VLANEnable": v(True)})
    lan = "InternetGatewayDevice.LANDevice.1."
    igd[f"{p}_PortBinding"] = {
        "1": {"WANInterface": v(f"{WCD}.1.WANIPConnection.1"),
              "LANInterface": v(f"{lan}LANEthernetInterfaceConfig.4,{lan}WLANConfiguration.2,{lan}WLANConfiguration.6")},
        "2": {"WANInterface": v(f"{WCD}.1.WANPPPConnection.2"),
              "LANInterface": v(",".join([f"{lan}LANEthernetInterfaceConfig.{i}" for i in (1, 2, 3)]
                                         + [f"{lan}WLANConfiguration.1", f"{lan}WLANConfiguration.5"]))},
    }
    igd["ManagementServer"] = {"ConnectionRequestURL": v("http://10.10.10.250:7547/")}
    return doc


def servicio(acs, monkeypatch):
    return ConfigOnuService(None, nbi="http://nbi", transporte=httpx.MockTransport(acs))


def correr(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ lectura
def test_huawei_se_lee_como_en_su_web():
    c = cfg.configuracion(huawei())
    assert c["marca"] == "huawei"
    tr069, fichas = c["wans"]
    assert tr069["tipo"] == "pppoe" and tr069["modo"] == "ruta" and tr069["vlan"] == 10 and tr069["tr069"]
    assert tr069["puertos"] == ["LAN1", "LAN2", "LAN3", "SSID1", "SSID5"]  # SSID5 solo sale del policy_route
    assert tr069["dmz"] == {"habilitada": False, "ip": None} and tr069["usuario"] == "Casa"
    assert fichas["modo"] == "puente" and fichas["vlan"] == 4 and not fichas["tr069"]
    assert fichas["puertos"] == ["LAN4", "SSID2", "SSID3"]
    bandas = {r["indice"]: (r["banda"], r["ancho"], r["seguridad"]) for r in c["wifi"]}
    assert bandas == {1: ("2.4 GHz", "0", "con contraseña"), 2: ("2.4 GHz", "0", "abierta"),
                      3: ("5 GHz", "3", "abierta"), 5: ("5 GHz", "3", "con contraseña")}
    assert c["wifi"][0]["canal"] == 0 and 13 in c["wifi"][0]["canales"] and 149 in c["wifi"][3]["canales"]
    assert c["lan"]["ip"] == "192.168.100.1" and c["lan"]["inicio"] == "192.168.100.2" and c["lan"]["dhcp"]
    assert c["puertos"] == ["LAN1", "LAN2", "LAN3", "LAN4", "SSID1", "SSID2", "SSID3", "SSID5"]


def test_zte_lee_enlaces_y_vlan_con_su_prefijo():
    c = cfg.configuracion(zte_completa())
    assert c["marca"] == "zte"
    por_nombre = {w["nombre"]: w for w in c["wans"]}
    assert por_nombre["Zona FdezNet"]["puertos"] == ["LAN4", "SSID2", "SSID6"] and por_nombre["Zona FdezNet"]["vlan"] == 4
    internet = por_nombre["Internet"]
    assert internet["tr069"] and internet["vlan"] == 10 and internet["puertos"] == ["LAN1", "LAN2", "LAN3", "SSID1", "SSID5"]
    assert {r["indice"]: r["banda"] for r in c["wifi"]}[6] == "5 GHz"


# ------------------------------------------------------------------ reglas puras
def test_la_wan_del_tr069_no_se_apaga_ni_cambia_de_vlan_ni_pasa_a_puente():
    igd = huawei()[IGD]
    ruta = f"{WCD}.1.WANPPPConnection.1"
    conexion = cfg._nodo(igd, ruta)
    for datos in ({"habilitada": False}, {"vlan": 20}, {"modo": "puente"}, {"servicio": "INTERNET"}):
        with pytest.raises(ValueError, match="TR-069"):
            cfg.valores_wan(conexion, ruta, datos, "huawei", None, True)
    valores = cfg.valores_wan(conexion, ruta, {"usuario": "cliente327", "clave": "x"}, "huawei", None, True)
    assert [ruta + ".Username", "cliente327", "xsd:string"] in valores and any(p[0].endswith(".Password") for p in valores)


def test_solo_se_manda_lo_que_cambia():
    actual = cfg.configuracion(huawei())["wans"][0]
    assert cfg.solo_cambios(actual, {"modo": "ruta", "vlan": 10, "nat": True, "usuario": "Casa", "clave": "",
                                     "dns": "10.10.10.1", "puertos": ["LAN3", "LAN1", "LAN2", "SSID5", "SSID1"]}) == {}
    assert cfg.solo_cambios(actual, {"vlan": 10, "usuario": "Otro"}) == {"usuario": "Otro"}


def test_zte_pone_la_vlan_con_su_prefijo():
    valores = cfg.valores_wan({}, f"{WCD}.1.WANIPConnection.3", {"vlan": 30, "modo": "puente"}, "zte", "X_ZTE-COM", False)
    campos = {p[0].rsplit(".", 1)[-1]: p[1] for p in valores}
    assert campos == {"ConnectionType": "IP_Bridged", "X_ZTE-COM_VLANEnable": True, "X_ZTE-COM_VLANID": 30}


def test_lan_valida_el_rango_del_dhcp():
    interfaz = f"{cfg.HOST}.IPInterface.1"
    with pytest.raises(ValueError, match="dentro"):
        cfg.valores_lan({"ip": "192.168.1.1", "inicio": "192.168.2.2", "fin": "192.168.2.50"}, interfaz)
    with pytest.raises(ValueError, match="no puede quedar"):
        cfg.valores_lan({"ip": "192.168.1.10", "inicio": "192.168.1.2", "fin": "192.168.1.50"}, interfaz)
    valores = dict((p[0].rsplit(".", 1)[-1], p[1]) for p in cfg.valores_lan(
        {"ip": "192.168.1.1", "inicio": "192.168.1.100", "fin": "192.168.1.200", "tiempo": 3600}, interfaz))
    assert valores["MinAddress"] == "192.168.1.100" and valores["DNSServers"] == "192.168.1.1" and valores["DHCPLeaseTime"] == 3600


def test_wifi_canal_fijo_y_ancho():
    wlan = huawei()[IGD]["LANDevice"]["1"]["WLANConfiguration"]["5"]
    valores = cfg.valores_wifi(5, wlan, {"canal": 44, "ancho": "1", "oculta": True}, "huawei", None)
    campos = {p[0].rsplit(".", 1)[-1]: p[1] for p in valores}
    assert campos == {"AutoChannelEnable": False, "Channel": 44, "X_HW_HT20": 1, "SSIDAdvertisementEnabled": False}
    with pytest.raises(ValueError):
        cfg.valores_wifi(5, wlan, {"ssid": "Señal"}, "huawei", None)


def test_un_puerto_va_por_una_sola_wan():
    nuevos = cfg.enlaces_nuevos({"a": ["LAN1", "LAN4"], "b": ["SSID2"]}, "b", ["LAN4", "SSID2"])
    assert nuevos == {"a": ["LAN1"], "b": ["LAN4", "SSID2"]}


# ------------------------------------------------------------------ con la ONU
def test_huawei_agrega_wan_puente_y_se_lleva_el_puerto(monkeypatch):
    acs = AcsFalso(huawei())
    s = servicio(acs, monkeypatch)
    resultado = correr(s.agregar_wan("dev", {"tipo": "ip", "modo": "puente", "vlan": 30, "puertos": ["LAN4"]}))
    nueva = acs.doc[IGD]["WANDevice"]["1"]["WANConnectionDevice"]["3"]["WANIPConnection"]["1"]
    assert nueva["ConnectionType"]["_value"] == "IP_Bridged" and nueva["X_HW_VLAN"]["_value"] == 30
    assert nueva["X_HW_SERVICELIST"]["_value"] == "INTERNET" and nueva["Enable"]["_value"] is True
    reglas = {r["WanName"]["_value"]: r["PhyPortName"]["_value"]
              for r in acs.doc[IGD]["Layer3Forwarding"]["X_HW_policy_route"].values()}
    assert reglas["wan1.3.ip1"] == "LAN4" and reglas["wan1.2.ip1"] == "SSID2,SSID3"
    fichas = acs.doc[IGD]["WANDevice"]["1"]["WANConnectionDevice"]["2"]["WANIPConnection"]["1"]["X_HW_LANBIND"]
    assert fichas["Lan4Enable"]["_value"] == 0
    assert {w["ruta"]: w["puertos"] for w in resultado["wans"]}[f"{WCD}.3.WANIPConnection.1"] == ["LAN4"]


def test_huawei_borra_la_wan_con_su_regla_pero_nunca_la_del_tr069(monkeypatch):
    acs = AcsFalso(huawei())
    s = servicio(acs, monkeypatch)
    with pytest.raises(ValueError, match="TR-069"):
        correr(s.borrar_wan("dev", f"{WCD}.1.WANPPPConnection.1"))
    correr(s.borrar_wan("dev", f"{WCD}.2.WANIPConnection.1"))
    assert "2" not in acs.doc[IGD]["WANDevice"]["1"]["WANConnectionDevice"]
    assert [r["WanName"]["_value"] for r in acs.doc[IGD]["Layer3Forwarding"]["X_HW_policy_route"].values()] == ["wan1.1.ppp1"]


def test_editar_la_wan_del_tr069_sin_cambiarla_no_manda_nada(monkeypatch):
    acs = AcsFalso(huawei())
    s = servicio(acs, monkeypatch)
    correr(s.guardar_wan("dev", f"{WCD}.1.WANPPPConnection.1", {"modo": "ruta", "vlan": 10, "habilitada": True}))
    assert not [t for t in acs.tareas if t["name"] == "setParameterValues"]


def test_zte_mueve_un_puerto_entre_enlaces(monkeypatch):
    acs = AcsFalso(zte_completa())
    s = servicio(acs, monkeypatch)
    correr(s.guardar_enlaces("dev", f"{WCD}.1.WANIPConnection.1", ["LAN3", "LAN4", "SSID2", "SSID6"]))
    enlaces = cfg.enlaces_actuales(acs.doc[IGD], "zte", "X_TELMEX_COM")
    assert enlaces[f"{WCD}.1.WANIPConnection.1"] == ["LAN3", "LAN4", "SSID2", "SSID6"]
    assert enlaces[f"{WCD}.1.WANPPPConnection.2"] == ["LAN1", "LAN2", "SSID1", "SSID5"]


def test_abrir_puerto_tcp_y_udp_crea_dos_reglas_y_se_cierran(monkeypatch):
    acs = AcsFalso(huawei())
    s = servicio(acs, monkeypatch)
    wan = f"{WCD}.1.WANPPPConnection.1"
    resultado = correr(s.abrir_puerto("dev", {"wan": wan, "externo": 8080, "interno": 80, "ip": "192.168.100.20", "protocolo": "ambos"}))
    reglas = resultado["wans"][0]["puertos_abiertos"]
    assert sorted(r["protocolo"] for r in reglas) == ["TCP", "UDP"] and reglas[0]["ip"] == "192.168.100.20"
    correr(s.cerrar_puerto("dev", reglas[0]["ruta"]))
    assert len(acs.doc[IGD]["WANDevice"]["1"]["WANConnectionDevice"]["1"]["WANPPPConnection"]["1"]["PortMapping"]) == 1
    with pytest.raises(ValueError, match="puente"):
        correr(s.abrir_puerto("dev", {"wan": f"{WCD}.2.WANIPConnection.1", "externo": 22, "ip": "192.168.100.20"}))


def test_un_rechazo_de_la_onu_deshace_la_wan_a_medias(monkeypatch):
    acs = AcsFalso(huawei(), rechaza=lambda t: t["name"] == "setParameterValues")
    s = servicio(acs, monkeypatch)
    antes = copy.deepcopy(acs.doc[IGD]["WANDevice"]["1"]["WANConnectionDevice"])
    with pytest.raises(ValueError, match="rechazó"):
        correr(s.agregar_wan("dev", {"tipo": "ip", "modo": "puente", "vlan": 30}))
    assert set(acs.doc[IGD]["WANDevice"]["1"]["WANConnectionDevice"]) == set(antes)


def test_ficha_del_equipo_muestra_la_serie_legible():
    doc = {"_id": "00259E-HG8145V5-48575443F1C8D69E", "_lastInform": "2026-11-07T17:35:23.941Z",
           "_deviceId": {"_Manufacturer": "Huawei Technologies Co., Ltd", "_ProductClass": "HG8145V5",
                         "_SerialNumber": "48575443F1C8D69E"},
           IGD: {"ManagementServer": {"ConnectionRequestURL": v("http://10.10.10.254:7547/")},
                 "DeviceInfo": {"SoftwareVersion": v("V5R019C10S386")}}}
    ficha = cfg.ficha_equipo(doc)
    assert ficha["serie"] == "HWTCF1C8D69E" and ficha["ip"] == "10.10.10.254" and ficha["firmware"] == "V5R019C10S386"
    assert cfg.ficha_equipo({"_id": "x", "_deviceId": {"_SerialNumber": "ZTEG24146156"}})["serie"] == "ZTEG24146156"


def test_x6_usa_su_banda_canales_y_160_mhz():
    wlan = {"SSID": v("FdezNet-FAAD"), "Enable": v(True), "AutoChannelEnable": v(True), "Channel": v(36),
            "ChannelsInUse": v("36"), "PossibleChannels": v("36,40,44,48,149"), "Standard": v("11ax"),
            "X_HW_Standard": v("11ax"), "X_HW_RFBand": v("5GHz"), "X_HW_HT20": v(4),
            "X_HW_SupportedStandards": v("a,n,ac,ax"), "X_HW_CurrentOperatingChannelBandwidth": v("160MHz")}
    igd = {"LANDevice": {"1": {"WLANConfiguration": {"5": wlan}}}}
    red = cfg.redes(igd, "huawei", None)[0]
    assert red["banda"] == "5 GHz" and red["canales"] == [36, 40, 44, 48, 149]
    assert red["anchos"]["4"] == "160 MHz" and "2" not in red["anchos"] and red["ancho_actual"] == "160MHz"
