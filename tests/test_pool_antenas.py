from types import SimpleNamespace

import pytest

from src.application.services.network_service import validar_red
from src.application.services.pool_antenas_service import (
    armar_mapa,
    ips_del_rango,
    resumen,
    siguiente_de_mapa,
)
from src.domain.schemas import RedCreate


def _red(**cambios):
    base = dict(id=9, nombre="Antenas FM", cidr="13.13.13.0/24", gateway="13.13.13.1",
                tipo="antenas", ip_inicio="13.13.13.11", ip_fin="13.13.13.20")
    base.update(cambios)
    return SimpleNamespace(**base)


def _por_ip(mapa):
    return {f["ip"]: f for f in mapa}


def test_el_rango_recorta_las_ip_repartibles():
    ips = [str(ip) for ip in ips_del_rango(_red())]
    assert ips[0] == "13.13.13.11" and ips[-1] == "13.13.13.20" and len(ips) == 10
    # Sin rango: todos los hosts de la red.
    assert len(ips_del_rango(_red(ip_inicio=None, ip_fin=None))) == 254


def test_mapa_con_clientes_equipos_bajas_y_lo_que_ve_el_mikrotik():
    clientes = [
        {"id": 1, "nombre": "Ana", "estado": "activo", "ip_antena": "13.13.13.11", "antena_responde": True},
        {"id": 2, "nombre": "Beto", "estado": "cancelado", "ip_antena": "13.13.13.12"},
        {"id": 3, "nombre": "Caro", "estado": "cancelado", "ip_antena": "13.13.13.13"},
        {"id": 4, "nombre": "Dani", "estado": "suspendido", "ip_antena": "13.13.13.11"},
        {"id": 5, "nombre": "Fuera", "estado": "activo", "ip_antena": "10.10.11.5"},
    ]
    equipos = [{"id": 1, "nombre": "FdezNet-90-sec2", "ip": "13.13.13.3"}]
    en_mikrotik = {"13.13.13.13": "Equipo en ARP: 60:22", "13.13.13.14": "Equipo en ARP: 70:A7", "13.13.13.1": "Dirección"}

    mapa = armar_mapa(_red(), clientes, equipos, en_mikrotik)
    filas = _por_ip(mapa)

    assert filas["13.13.13.1"]["estado"] == "gateway"
    assert filas["13.13.13.3"]["estado"] == "equipo" and filas["13.13.13.3"]["en_rango"] is False
    assert filas["13.13.13.11"]["estado"] == "cliente" and filas["13.13.13.11"]["detalle"] == "Ana"
    assert filas["13.13.13.11"]["repetida_con"] == ["Dani"]
    assert filas["13.13.13.12"]["estado"] == "baja"
    assert filas["13.13.13.13"]["estado"] == "baja" and filas["13.13.13.13"]["visto_en_mikrotik"]
    assert filas["13.13.13.14"]["estado"] == "mikrotik"
    assert filas["13.13.13.15"]["estado"] == "libre"
    assert "10.10.11.5" not in filas

    # La de una baja que el MikroTik ya no ve se vuelve a dar primero (orden de IP);
    # la que aún se ve (13) no.
    assert siguiente_de_mapa(mapa) == "13.13.13.12"
    assert siguiente_de_mapa(mapa, excluir={"13.13.13.12"}) == "13.13.13.15"
    cuenta = resumen(mapa)
    assert cuenta["cliente"] == 1 and cuenta["baja"] == 2 and cuenta["mikrotik"] == 1
    assert cuenta["disponibles"] == 1 + 6  # la 12 y de la 15 a la 20


def test_pool_lleno():
    clientes = [{"id": i, "nombre": f"C{i}", "estado": "activo", "ip_antena": f"13.13.13.{i}"} for i in range(11, 21)]
    assert siguiente_de_mapa(armar_mapa(_red(), clientes, [], {})) is None


def test_validar_red_revisa_rango_y_gateway():
    base = dict(nombre="A", cidr="12.12.12.0/24", gateway="12.12.12.1", router_id=2, tipo="antenas")
    validar_red(RedCreate(**base, ip_inicio="12.12.12.15", ip_fin="12.12.12.254"))
    with pytest.raises(ValueError, match="no está dentro"):
        validar_red(RedCreate(**base, ip_inicio="13.13.13.15"))
    with pytest.raises(ValueError, match="menor que el fin"):
        validar_red(RedCreate(**base, ip_inicio="12.12.12.200", ip_fin="12.12.12.15"))
    # Vacíos del formulario cuentan como sin rango.
    assert RedCreate(**base, ip_inicio="", ip_fin=" ").ip_inicio is None
