"""Detalle y reinicio de una ONU por la API web de la OLT VSOL."""

import asyncio
import urllib.parse
from types import SimpleNamespace

import pytest

from src.application.services.vsol_api_service import VsolApiService, interpretar_causa_caida

OLT = SimpleNamespace(id=7, ip="12.12.12.2", api_user="u", api_password="p", api_protocol="https", api_port=443)


class _OLTFalsa(VsolApiService):
    """Responde como el panel web; guarda cada POST que recibe."""

    def __init__(self, serial_en_olt="HWTC0000AAAA"):
        self.db = SimpleNamespace(get=lambda *_a: asyncio.sleep(0, OLT))
        self.serial_en_olt = serial_en_olt
        self.posts = []

    def _abrir_sesion_sync(self, olt):
        return object(), "https://olt", False

    def _descubrir_payloads_pon_sync(self, opener, base_url, verify_ssl):
        return [b"slotid=0&portid=1&onuid=1&port_id=5", b"slotid=0&portid=2&onuid=1&port_id=6"]

    def _request_json_sync(self, opener, url, method="GET", data=None, verify_ssl=False):
        accion = url.split("/action/")[1].split("?")[0]
        datos = dict(urllib.parse.parse_qsl(data.decode())) if data else {}
        if method == "POST":
            self.posts.append((accion, datos))
        if accion == "gpononuauthinfo" and "who" not in datos:
            return {"retcode": "0", "data": {"onuAuth_list": [
                {"pon_id": datos["portid"], "onu_id": f"GPON0/{datos['portid']}:3", "info": self.serial_en_olt},
            ]}}
        if accion == "gpononuauthinfo":
            return {"retcode": "0", "data": {}}
        if accion == "gpononudetail":
            return {"retcode": "0", "data": {"onu_detail_info": [
                {"property": "Main Software Version", "value": "V5R022C00S266"},
                {"property": "System Uptime", "value": "89035 s"},
                {"property": "Operate Status", "value": "enable"},
            ]}}
        if accion == "gpononuoptical":
            return {"retcode": "0", "data": {"onu_optical_info": [
                {"property": "Distance", "value": "1440"},
                {"property": "Temperature", "value": "40.00"},
                {"property": "Power Feed Voltage", "value": "3.32(V)"},
                {"property": "Rx Optical Level(ONU)", "value": "-19.96"},
                {"property": "Lower Rx Optical Threshold", "value": "-25.00"},
            ]}}
        if accion == "gpononutimetampdetail":
            return {"retcode": "0", "data": {"offOnuReasonDtail_list": [
                {"time": "2026/10/01 21:10:05", "reason": "Power Off"},
                {"time": "2026/09/28 08:00:00", "reason": "ONU Signal LOS"},
                {"time": "N/A", "reason": "N/A"},
            ]}}
        raise AssertionError(url)


def test_el_reinicio_siempre_usa_who_1_nunca_el_0_que_borra():
    assert VsolApiService.payload_reinicio_onu(2, 3) == {"who": 1, "slotid": 0, "portid": 2, "onuid": 3}


def test_reinicia_la_onu_si_el_serial_coincide():
    olt = _OLTFalsa()
    resultado = asyncio.run(olt.reiniciar_onu(7, 2, 3, "hwtc0000aaaa"))
    assert resultado["reiniciada"] is True
    escrituras = [(a, d) for a, d in olt.posts if "who" in d]
    assert escrituras == [("gpononuauthinfo", {"who": "1", "slotid": "0", "portid": "2", "onuid": "3"})]


def test_no_reinicia_si_en_ese_lugar_ya_hay_otra_onu():
    olt = _OLTFalsa(serial_en_olt="HWTC0000BBBB")
    with pytest.raises(ValueError, match="ya no es la misma"):
        asyncio.run(olt.reiniciar_onu(7, 2, 3, "HWTC0000AAAA"))
    assert all("who" not in d for _a, d in olt.posts)


def test_no_acepta_numeros_de_onu_invalidos():
    with pytest.raises(ValueError):
        asyncio.run(_OLTFalsa().reiniciar_onu(7, 2, 0, "HWTC0000AAAA"))


def test_detalle_trae_distancia_temperatura_voltaje_y_caidas():
    detalle = asyncio.run(_OLTFalsa().detalle_onu(7, 1, 2))
    assert detalle["distancia_m"] == 1440
    assert detalle["temperatura_c"] == 40.0
    assert detalle["voltaje_v"] == 3.32
    assert detalle["rx_dbm"] == -19.96 and detalle["rx_minimo_dbm"] == -25.0
    assert detalle["encendida_segundos"] == 89035
    assert detalle["firmware"] == "V5R022C00S266"
    assert [c["tipo"] for c in detalle["historial_caidas"]] == ["corte_luz", "fibra"]


@pytest.mark.parametrize("razon, tipo", [
    ("Power Off", "corte_luz"), ("ONU Signal LOS", "fibra"), ("ONU PLOAM LOS", "fibra"), ("Algo nuevo", "otra"),
])
def test_interpreta_la_causa_de_la_caida(razon, tipo):
    assert interpretar_causa_caida(razon)["tipo"] == tipo


@pytest.mark.parametrize("razon", [None, "", "N/A"])
def test_sin_causa_no_inventa_nada(razon):
    assert interpretar_causa_caida(razon) is None
