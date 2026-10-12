"""Panel de la OLT: mismas acciones que el panel web VSOL, sin guardar y confirmando el serial."""

import asyncio
from types import SimpleNamespace

import pytest

from src.application.services.panel_olt_service import (
    PanelOltService,
    fila_onu,
    payload_eliminar_pon,
    payload_eliminar_varias,
    payload_fila,
)
from test_vsol_onu_acciones import _OLTFalsa

OLT = SimpleNamespace(id=7, nombre="Vicente Guerrero", api_enabled=True, ip="1", api_user="u",
                      api_password="p", api_protocol="https", api_port=443)


class _DB:
    def __init__(self):
        self.agregados = []

    async def get(self, _modelo, _llave):
        return OLT

    def add(self, objeto):
        self.agregados.append(objeto)

    async def commit(self):
        return None


def _panel(serial="HWTC0000AAAA"):
    db = _DB()
    olt = _OLTFalsa(serial_en_olt=serial)
    olt.db = db
    return PanelOltService(db, olt), olt, db


def _escrituras(olt):
    return [(a, d) for a, d in olt.posts if a != "loginout" and ("who" in d or a == "configsave")]


@pytest.mark.parametrize("accion, who", [
    ("eliminar", 0), ("reiniciar", 1), ("marcar", 5), ("desmarcar", 6), ("activar", 20), ("desactivar", 21),
])
def test_cada_boton_manda_el_who_del_panel(accion, who):
    datos = payload_fila(accion, 4, 2)
    assert datos["who"] == who and (datos["portid"], datos["onuid"], datos["slotid"]) == (4, 2, 0)
    assert ("select" in datos) == (accion == "eliminar")


def test_accion_desconocida_no_llega_a_la_olt():
    with pytest.raises(ValueError, match="no válida"):
        payload_fila("reset_fabrica", 4, 2)


def test_eliminar_varias_y_por_pon_como_el_panel():
    assert payload_eliminar_varias([{"pon": 4, "onuid": 2}, {"pon": 1, "onuid": 9}]) == {
        "who": 3, "select1": "slot[0]_pon[4]_onu[2]", "select2": "slot[0]_pon[1]_onu[9]", "selectNums": 2}
    assert payload_eliminar_pon(4, solo_offline=True)["who"] == 4
    assert payload_eliminar_pon(4, solo_offline=False)["who"] == 2


def test_la_fila_trae_las_columnas_del_panel():
    fila = fila_onu(
        {"pon_id": "4", "onuid": "2", "onu_id": "GPON0/4:2", "state": "1", "description": "Teresa 5BAF",
         "attention_status": "0", "model": "HG8145X6-10", "profile": "default", "mode": "SN",
         "info": "HWTCdec861ad", "active_action_state": "Deactivate"},
        {"rx_power": "-15.41", "tx_power": "2.32"},
        {"alive_time": "3 09:43:53"},
    )
    assert (fila["onu_id"], fila["online"], fila["modelo"], fila["serial"], fila["rx"]) == (
        "GPON0/4:2", True, "HG8145X6-10", "HWTCdec861ad", "-15.41")
    assert fila["activa"] is True and fila["marcada"] is False and fila["tiempo_activa"] == "3 09:43:53"


def test_borra_una_onu_en_linea_con_cliente_sin_guardar_y_queda_en_bitacora():
    panel, olt, db = _panel()
    asyncio.run(panel.accion(7, 2, 3, "eliminar", "hwtc0000aaaa", SimpleNamespace(id=1, usuario="admin")))
    assert _escrituras(olt) == [
        ("gpononuauthinfo", {"who": "0", "slotid": "0", "portid": "2", "onuid": "3", "select": "2"}),
    ]
    assert db.agregados[0].accion == "Panel OLT: Eliminar ONU"


def test_no_toca_la_onu_si_en_ese_lugar_hay_otra():
    panel, olt, _ = _panel(serial="HWTC0000BBBB")
    with pytest.raises(ValueError, match="ya no está"):
        asyncio.run(panel.accion(7, 2, 3, "desactivar", "HWTC0000AAAA"))
    assert _escrituras(olt) == []


def test_el_reinicio_queda_como_reinicio_para_radar():
    panel, _, db = _panel()
    asyncio.run(panel.accion(7, 2, 3, "reiniciar", "HWTC0000AAAA"))
    assert db.agregados[0].ruta.endswith("/onus/2/3/reiniciar")


def test_cambia_la_descripcion_como_el_panel_y_respeta_32():
    panel, olt, _ = _panel()
    asyncio.run(panel.descripcion(7, 2, 3, "HWTC0000AAAA", "  Teresa Gomez 5BAF "))
    assert _escrituras(olt) == [("gpononudetail", {"submit": "Submit", "who": "0", "slotid": "0", "ponid": "2",
                                                   "onuid": "3", "onu_description": "Teresa Gomez 5BAF"})]
    with pytest.raises(ValueError, match="32"):
        asyncio.run(panel.descripcion(7, 2, 3, "HWTC0000AAAA", "x" * 33))


def test_eliminar_varias_confirma_cada_serial():
    panel, olt, _ = _panel()
    asyncio.run(panel.eliminar_varias(7, [{"pon": 2, "onuid": 3, "serial": "HWTC0000AAAA"}]))
    assert _escrituras(olt) == [("gpononuauthinfo", {"who": "3", "select1": "slot[0]_pon[2]_onu[3]", "selectNums": "1"})]


def test_el_isp_que_quiere_guardar_lo_activa(monkeypatch):
    monkeypatch.setenv("OLT_GUARDAR_CONFIGURACION", "1")
    panel, olt, _ = _panel()
    asyncio.run(panel.accion(7, 2, 3, "desactivar", "HWTC0000AAAA"))
    asyncio.run(panel.accion(7, 2, 3, "reiniciar", "HWTC0000AAAA"))
    assert [a for a, _ in _escrituras(olt)].count("configsave") == 1  # reiniciar no guarda


from src.application.services.panel_olt_service import (  # noqa: E402
    PanelOltDetalle,
    payload_alta_manual,
    payload_modificar,
    payload_perfil,
)


def test_alta_manual_por_sn_como_el_panel():
    datos = payload_alta_manual(4, 1, {"modo": "0", "sn": "hwtc0b46d3af", "perfil_onu": "default",
                                       "perfil_linea": "line_INTERNET", "perfil_servicio": "srv_INTERNET",
                                       "perfil_alarma": "alarm_INTERNET"})
    assert datos == {"slot_select": 0, "port_select": 4, "slotid": 0, "portid": 4, "onuid": 1, "authmode": "0",
                     "gponsn": "HWTC0B46D3AF", "onutype": "default", "s_linePro": "line_INTERNET",
                     "s_srvPro": "srv_INTERNET", "s_alarmPro": "alarm_INTERNET", "s_fmtPro": "N/A", "who": 0}


@pytest.mark.parametrize("datos, error", [
    ({"modo": "9"}, "modo"),
    ({"modo": "0"}, "Falta"),
    ({"modo": "2", "loid": "abc"}, "Falta"),
])
def test_alta_manual_pide_lo_necesario(datos, error):
    with pytest.raises(ValueError, match=error):
        payload_alta_manual(4, 1, datos)


def test_modificar_sn_loid_y_perfil_como_el_panel():
    assert payload_modificar(4, 2, "0", "hwtc0b46d3af") == {
        "authmode": "0", "who": 0, "slotid": 0, "ponid": 4, "onuid": 2, "gponsn": "HWTC0B46D3AF"}
    assert payload_modificar(4, 2, "1", "abc")["loid"] == "abc"
    assert payload_perfil(4, 2, "default") == {"who": 1, "slotid": 0, "ponid": 4, "onuid": 2, "onuptype": "default"}
    with pytest.raises(ValueError):
        payload_modificar(4, 2, "0", "")


class _OLTModifica(_OLTFalsa):
    def _request_json_sync(self, opener, url, method="GET", data=None, verify_ssl=False):
        if "/action/gpononumodify" in url:
            import urllib.parse as up
            self.posts.append(("gpononumodify", dict(up.parse_qsl(data.decode()))))
            return {"retcode": "0", "data": {}}
        return super()._request_json_sync(opener, url, method, data, verify_ssl)


def test_modificar_confirma_el_serial_y_no_guarda():
    db = _DB()
    olt = _OLTModifica()
    olt.db = db
    asyncio.run(PanelOltDetalle(db, olt).modificar(7, 2, 3, "HWTC0000AAAA", {"modo": "0", "valor": "HWTC0000CCCC", "perfil": "default"}))
    escrituras = [a for a, _ in olt.posts if a in ("gpononumodify", "configsave")]
    assert escrituras == ["gpononumodify", "gpononumodify"]
    assert db.agregados[0].accion == "Panel OLT: Modificar ONU"

    otra = _OLTModifica(serial_en_olt="HWTC0000BBBB")
    otra.db = db
    with pytest.raises(ValueError, match="ya no está"):
        asyncio.run(PanelOltDetalle(db, otra).modificar(7, 2, 3, "HWTC0000AAAA", {"valor": "X"}))
