"""Abrir la ONU, el router de radio o la antena del cliente desde el admin."""

import asyncio
from datetime import datetime

from src.application.services import acceso_remoto_service as acceso
from src.application.services.acceso_remoto_service import (
    AccesoRemotoService,
    comentario_puente,
    puente_vencido,
    puertos_a_probar,
    reglas_puente,
    url_de,
)

CONFIG = {
    "huawei": {"url": "", "cuentas": "", "puerto": "80"},
    "zte": {"url": "", "cuentas": "", "puerto": "8090"},
    "radio": {"puerto": "8080"},
    "antena": {"puerto": "443"},
}


def test_cada_marca_prueba_primero_su_puerto():
    assert puertos_a_probar(CONFIG, "huawei", False) == ["80", "8090", "8080"]
    assert puertos_a_probar(CONFIG, "zte", False) == ["8090", "80", "8080"]
    assert puertos_a_probar(CONFIG, None, True) == ["8080", "80", "8090"]
    # EPON por MAC o Nokia: no se sabe la marca, se empieza por el 80.
    assert puertos_a_probar(CONFIG, None, False) == ["80", "8090", "8080"]


def test_router_de_radio_sin_puerto_usa_el_80():
    config = {**CONFIG, "radio": {"puerto": ""}}
    assert puertos_a_probar(config, None, True) == ["80", "8090"]


def test_gana_el_primero_de_la_lista_que_contesta(monkeypatch):
    async def responde(_ip, puerto):
        return puerto in {"80", "8090"}

    monkeypatch.setattr(acceso, "_responde", responde)
    assert asyncio.run(acceso.primer_puerto_que_responde("10.10.9.32", ["8090", "80", "8080"])) == "8090"
    assert asyncio.run(acceso.primer_puerto_que_responde("10.10.9.32", ["8080", "80"])) == "80"
    assert asyncio.run(acceso.primer_puerto_que_responde("10.10.9.32", ["8080"])) is None


def test_url_omite_el_puerto_de_siempre():
    assert url_de("10.10.9.2", "80") == "http://10.10.9.2"
    assert url_de("10.10.9.2", "8090") == "http://10.10.9.2:8090"
    assert url_de("10.8.0.4", "18195", https=True) == "https://10.8.0.4:18195"
    assert url_de("192.168.21.5", "443") == "https://192.168.21.5"


def test_puente_dirige_el_puerto_del_mikrotik_a_la_antena():
    reglas = reglas_puente("10.8.0.4", 18195, "192.168.1.20", "443", "FDEZNET-ANTENA 195 2026-10-19T15:30")
    assert reglas["dstnat"]["dst-address"] == "10.8.0.4"
    assert reglas["dstnat"]["dst-port"] == "18195"
    assert reglas["dstnat"]["to-addresses"] == "192.168.1.20"
    assert reglas["dstnat"]["to-ports"] == "443"
    assert reglas["srcnat"]["action"] == "masquerade"
    assert reglas["forward"]["connection-nat-state"] == "dstnat"


def test_el_puente_vence_a_su_hora_y_no_toca_otras_reglas():
    comentario = comentario_puente(195, datetime(2026, 10, 19, 15, 30))
    assert comentario == "FDEZNET-ANTENA 195 2026-10-19T15:30"
    assert not puente_vencido(comentario, datetime(2026, 10, 19, 15, 29))
    assert puente_vencido(comentario, datetime(2026, 10, 19, 15, 30))
    assert not puente_vencido("CORTE_FDEZNET", datetime(2030, 1, 1))
    assert not puente_vencido(None, datetime(2030, 1, 1))


class _MK:
    def __init__(self, reglas):
        self.reglas = reglas
        self.llamadas = []

    def _request(self, metodo, ruta, payload=None, raise_on_error=False):
        self.llamadas.append((metodo, ruta, payload))
        if metodo == "GET":
            return self.reglas.get(ruta, [])
        if metodo == "PUT":
            return {".id": "*NUEVA"}
        return True

    def _mover_al_inicio(self, ruta, cadena, ids):
        self.llamadas.append(("MOVE", ruta, ids))


def test_abrir_otra_vez_reemplaza_el_puente_del_mismo_cliente():
    mk = _MK({
        "/ip/firewall/nat": [
            {".id": "*1", "comment": "FDEZNET-ANTENA 195 2026-10-19T15:00"},
            {".id": "*2", "comment": "FDEZNET-ANTENA 33 2026-10-19T15:00"},
            {".id": "*3", "comment": "CORTE_FDEZNET"},
        ],
    })
    reglas = reglas_puente("10.8.0.4", 18195, "192.168.1.20", "443", "FDEZNET-ANTENA 195 2026-10-19T15:30")
    AccesoRemotoService._crear_puente(mk, 195, reglas)
    borradas = [ruta for metodo, ruta, _ in mk.llamadas if metodo == "DELETE"]
    assert borradas == ["/ip/firewall/nat/*1"]
    assert [m for m, _, _ in mk.llamadas].count("PUT") == 3
    assert ("MOVE", "/ip/firewall/filter", ["*NUEVA"]) in mk.llamadas


def test_se_cierran_solo_los_puentes_vencidos():
    mk = _MK({
        "/ip/firewall/nat": [
            {".id": "*1", "comment": "FDEZNET-ANTENA 195 2026-10-19T15:00"},
            {".id": "*2", "comment": "FDEZNET-ANTENA 33 2026-10-19T16:00"},
            {".id": "*3", "comment": "CORTE_FDEZNET"},
        ],
        "/ip/firewall/filter": [{".id": "*9", "comment": "FDEZNET-ANTENA 195 2026-10-19T15:00"}],
    })
    assert AccesoRemotoService._cerrar_vencidos(mk, datetime(2026, 10, 19, 15, 10)) == 2
    borradas = [ruta for metodo, ruta, _ in mk.llamadas if metodo == "DELETE"]
    assert borradas == ["/ip/firewall/nat/*1", "/ip/firewall/filter/*9"]
