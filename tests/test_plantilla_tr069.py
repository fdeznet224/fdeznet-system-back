"""Plantillas TR-069: el equipo que llega solo con la WAN del ACS queda como dice su plantilla."""

import asyncio
import copy
from types import SimpleNamespace

import httpx
import pytest

from src.application.services import plantilla_tr069_service as pl
from src.application.services.config_onu_service import configuracion
from src.application.services.plantilla_tr069_service import PlantillaTr069Service
from test_config_onu import IGD, WCD, huawei, zte_completa
from test_plantilla_zte import AcsFalso, v

CLIENTE = {"id": 327, "nombre": "Prueba", "usuario_pppoe": "cliente327", "clave_pppoe": "x1", "ssid": "FdezNet-187B",
           "clave_wifi": "secreta123", "pendiente": False}


def definicion_de(doc):
    return pl.validar(pl.desde_config(configuracion(doc)))


def huawei_restablecida():
    """La HG8145V5 recién restablecida con la plantilla mínima: solo la WAN del ACS, WiFi de fábrica."""
    doc = huawei()
    igd = doc[IGD]
    del igd["WANDevice"]["1"]["WANConnectionDevice"]["2"]
    ppp = igd["WANDevice"]["1"]["WANConnectionDevice"]["1"]["WANPPPConnection"]["1"]
    ppp["Username"] = v("FdezNet")
    ppp["X_HW_LANBIND"] = {**{f"Lan{i}Enable": v(1) for i in range(1, 5)}, **{f"SSID{i}Enable": v(1 if i == 1 else 0) for i in range(1, 5)}}
    igd["Layer3Forwarding"]["X_HW_policy_route"] = {
        "1": {"PolicyRouteType": v("SourcePhyPort"), "WanName": v("wan1.1.ppp1"), "PhyPortName": v("LAN1,LAN2,LAN3,LAN4,SSID1,SSID5")}}
    wifi = igd["LANDevice"]["1"]["WLANConfiguration"]
    for i in ("1", "5"):
        wifi[i]["SSID"] = v("HUAWEI-2.4G-ABCD" if i == "1" else "HUAWEI-5G-ABCD")
    for i in ("2", "3"):
        wifi[i].update(SSID=v(f"SSID{i}"), Enable=v(False), BeaconType=v("WPAand11i"))
    return doc


def aplicar(doc, definicion, cliente=CLIENTE, rechaza=lambda t: False):
    acs = AcsFalso(doc, rechaza)
    servicio = PlantillaTr069Service(None, nbi="http://nbi", transporte=httpx.MockTransport(acs))

    async def correr():
        async with servicio.cfg._http() as http:
            return await servicio._aplicar(http, "dev", definicion, cliente)

    return acs, asyncio.run(correr())


def test_la_plantilla_copiada_de_un_equipo_bien_configurado_dice_que_esta_conforme():
    definicion = definicion_de(huawei())
    acs, fichas = definicion["wans"]
    assert acs["acs"] and acs["vlan"] == 10 and acs["puertos"] == ["LAN1", "LAN2", "LAN3", "red:cliente"]
    assert not fichas["acs"] and fichas["modo"] == "puente" and fichas["vlan"] == 4
    assert fichas["puertos"] == ["LAN4", "red:Zona FdezNet"]
    assert [r["nombre"] for r in definicion["redes"]] == ["cliente", "Zona FdezNet"]
    assert pl.lo_que_falta(configuracion(huawei()), definicion, None) == []
    assert pl.lo_que_falta(configuracion(zte_completa()), definicion_de(zte_completa()), None) == []


def test_huawei_restablecida_queda_como_la_plantilla_con_los_datos_del_cliente():
    definicion = definicion_de(huawei())
    doc = huawei_restablecida()
    faltan_antes = pl.lo_que_falta(configuracion(doc), definicion, CLIENTE)
    assert any("Zona FdezNet" in f for f in faltan_antes) and any("PPPoE" in f for f in faltan_antes)
    acs, resultado = aplicar(doc, definicion)
    assert resultado["faltan"] == [], resultado
    c = configuracion(acs.doc)
    zona = next(w for w in c["wans"] if w["modo"] == "puente")
    assert zona["vlan"] == 4 and zona["puertos"] == ["LAN4", "SSID2", "SSID3"]
    internet = next(w for w in c["wans"] if w["tr069"])
    assert internet["puertos"] == ["LAN1", "LAN2", "LAN3", "SSID1", "SSID5"] and internet["usuario"] == "cliente327"
    redes = {r["indice"]: (r["ssid"], r["habilitada"], r["seguridad"]) for r in c["wifi"]}
    assert redes[1][0] == redes[5][0] == "FdezNet-187B"
    assert redes[2] == ("Zona FdezNet", True, "abierta") and redes[3] == ("Zona FdezNet", True, "abierta")
    # El PPPoE va al final: al cambiarlo la ONU se reconecta.
    ultima = [t for t in acs.tareas if t["name"] == "setParameterValues"][-1]
    assert ultima["parameterValues"][0][0].endswith(".Username")
    wifi1 = acs.doc[IGD]["LANDevice"]["1"]["WLANConfiguration"]["1"]
    assert wifi1["PreSharedKey"]["1"]["KeyPassphrase"]["_value"] == "secreta123"


def test_un_equipo_conforme_no_recibe_cambios():
    acs, resultado = aplicar(huawei(), definicion_de(huawei()), cliente=None)
    assert resultado == {"hecho": [], "faltan": []}
    assert not [t for t in acs.tareas if t["name"] in ("setParameterValues", "addObject", "deleteObject")]


def test_sin_datos_del_cliente_no_toca_su_wifi_ni_su_pppoe():
    acs, resultado = aplicar(huawei_restablecida(), definicion_de(huawei()), cliente=None)
    assert resultado["faltan"] == []
    c = configuracion(acs.doc)
    assert next(w for w in c["wans"] if w["tr069"])["usuario"] == "FdezNet"
    assert {r["indice"]: r["ssid"] for r in c["wifi"]}[1] == "HUAWEI-2.4G-ABCD"


def test_validar_rechaza_plantillas_mal_hechas():
    base = pl.desde_config(configuracion(huawei()))
    sin_acs = copy.deepcopy(base)
    sin_acs["wans"][0]["acs"] = False
    with pytest.raises(ValueError, match="ACS"):
        pl.validar(sin_acs)
    repetido = copy.deepcopy(base)
    repetido["wans"][1]["puertos"].append("LAN1")
    with pytest.raises(ValueError, match="dos WAN"):
        pl.validar(repetido)
    red_falsa = copy.deepcopy(base)
    red_falsa["wans"][1]["puertos"].append("red:NoExiste")
    with pytest.raises(ValueError, match="no válido"):
        pl.validar(red_falsa)


def test_elige_la_de_su_modelo_y_si_no_la_general_de_su_marca():
    general = SimpleNamespace(marca="huawei", modelos="", nombre="General")
    x6 = SimpleNamespace(marca="huawei", modelos="HG8145X6", nombre="X6")
    zte = SimpleNamespace(marca="zte", modelos="", nombre="ZTE")
    plantillas = [general, x6, zte]
    assert pl.elegir(plantillas, "huawei", "HG8145X6%2D10") is x6
    assert pl.elegir(plantillas, "huawei", "HG8145V5") is general
    assert pl.elegir(plantillas, "zte", "F670L") is zte
    assert pl.elegir([x6], "huawei", "HG8145V5") is None
    todas = SimpleNamespace(marca="todas", modelos="", nombre="Todas")
    assert pl.elegir([todas], "zte", "F670L") is todas and pl.elegir([todas], "huawei", "HG8145V5") is todas
    assert pl.elegir([todas, x6], "huawei", "HG8145X6%2D10") is x6


def test_la_misma_plantilla_general_deja_bien_una_huawei_y_una_zte():
    """Copiada de la Huawei del dueño, también sirve para la ZTE (el sistema la traduce)."""
    definicion = definicion_de(huawei())
    assert pl.lo_que_falta(configuracion(zte_completa()), definicion, None) == []


def test_marca_por_el_primer_aviso():
    assert pl.marca_del_documento({"_deviceId": {"_Manufacturer": "ZTE", "_SerialNumber": "ZTEG24146156"}}) == "zte"
    assert pl.marca_del_documento({"_deviceId": {"_Manufacturer": "Huawei Technologies Co., Ltd"}}) == "huawei"


def test_zte_restablecida_queda_bien_con_la_plantilla_general_copiada_de_la_huawei():
    from test_plantilla_zte import zte
    acs, resultado = aplicar(zte(), definicion_de(huawei()), cliente=None)
    assert resultado["faltan"] == [], resultado
    c = configuracion(acs.doc)
    zona = next(w for w in c["wans"] if w["modo"] == "puente")
    assert zona["vlan"] == 4 and zona["puertos"] == ["LAN4", "SSID2", "SSID6"]
    internet = next(w for w in c["wans"] if w["tr069"])
    assert internet["puertos"] == ["LAN1", "LAN2", "LAN3", "SSID1", "SSID5"]
    vlan = acs.doc[IGD]["WANDevice"]["1"]["WANConnectionDevice"]["1"]["WANIPConnection"]["1"]
    assert vlan["X_TELMEX_COM_VLANID"]["_value"] == 4  # traducida al prefijo de esa ZTE


def test_si_la_zte_rechaza_un_enlace_sigue_con_su_wifi_y_su_pppoe_y_no_deja_enlaces_vacios():
    """ZTE de Sindy, 7 nov: un 9003 en el enlace de puertos detenía toda la plantilla y dejaba uno vacío."""
    from test_plantilla_zte import zte

    def rechaza(tarea):
        return any(r.endswith(".LANInterface") for r, _, _ in tarea.get("parameterValues") or [])

    acs, resultado = aplicar(zte(), definicion_de(huawei()), rechaza=rechaza)
    assert any("enlace" in f or "puerto" in f.lower() for f in resultado["faltan"]), resultado
    c = configuracion(acs.doc)
    assert next(w for w in c["wans"] if w["tr069"])["usuario"] == "cliente327"
    assert {r["indice"]: r["ssid"] for r in c["wifi"]}[1] == "FdezNet-187B"
    enlaces = acs.doc[IGD].get("X_TELMEX_COM_PortBinding") or acs.doc[IGD].get("X_ZTE-COM_PortBinding") or {}
    assert not [n for n, e in enlaces.items() if n.isdigit() and not e["WANInterface"]["_value"]]


def test_la_zte_reutiliza_un_enlace_vacio_en_vez_de_crear_otro():
    from test_plantilla_zte import zte

    doc = zte()
    igd = doc[IGD]
    clave = "X_TELMEX_COM_PortBinding" if "X_TELMEX_COM_PortBinding" in igd else "X_ZTE-COM_PortBinding"
    enlaces = igd.setdefault(clave, {})
    vacio = str(max([int(k) for k in enlaces if k.isdigit()] + [0]) + 1)
    enlaces[vacio] = {"WANInterface": v(""), "LANInterface": v("")}
    def creados(acs):
        return sum(t["name"] == "addObject" and "PortBinding" in t["objectName"] for t in acs.tareas)

    sin_vacio, _ = aplicar(zte(), definicion_de(huawei()), cliente=None)
    acs, resultado = aplicar(doc, definicion_de(huawei()), cliente=None)
    assert resultado["faltan"] == [], resultado
    assert creados(acs) == creados(sin_vacio) - 1
