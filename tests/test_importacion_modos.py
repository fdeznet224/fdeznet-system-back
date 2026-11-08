"""Excel de importación según el modo del router (PPPoE, DHCP o IP fija)."""
from types import SimpleNamespace

from src.application.services import importacion_service as imp

PLANES = [
    SimpleNamespace(id=1, nombre="Básico", velocidad_subida=5120, velocidad_bajada=10240,
                    burst_subida=0, burst_bajada=0, burst_time=0, prioridad=8),
    SimpleNamespace(id=2, nombre="Plus", velocidad_subida=10240, velocidad_bajada=30720,
                    burst_subida=0, burst_bajada=0, burst_time=0, prioridad=8),
]
BASE = {"id_red": 1, "id_zona": 2, "id_plantilla_facturacion": 3, "id_plan_sugerido": 9, "id_olt": 0}


def test_las_columnas_no_traen_pppoe_si_el_router_no_lo_usa():
    for modo in ("dhcp", "estatica"):
        columnas = imp.columnas_plantilla(modo)
        assert "usuario_pppoe" not in columnas and "password_pppoe" not in columnas
        assert {"mac_antena", "ip_antena", "tipo_conexion"} <= set(columnas)
    assert "usuario_pppoe" in imp.columnas_plantilla("pppoe")


def test_dhcp_toma_los_leases_de_la_red_con_su_mac_y_plan():
    leases = [
        {"address": "10.0.0.20", "mac-address": "aa:bb:cc:dd:ee:01", "host-name": "antena-juan",
         "rate-limit": "10000000/30000000"},
        {"address": "10.0.0.21", "mac-address": "AA-BB-CC-DD-EE-02", "dynamic": "true"},
        {"address": "192.168.1.5", "mac-address": "AA:BB:CC:DD:EE:03"},  # otra red
        {"address": "10.0.0.22", "mac-address": ""},
    ]
    filas = imp.filas_dhcp(leases, BASE, PLANES, 9, "10.0.0.0/24")
    assert [f["ip_asignada"] for f in filas] == ["10.0.0.20", "10.0.0.21"]
    assert filas[0]["mac_antena"] == "AA:BB:CC:DD:EE:01" and filas[0]["id_plan_sugerido"] == 2
    assert filas[0]["nombre_cliente"] == "antena-juan" and filas[0]["tipo_conexion"] == "antena"
    assert filas[1]["mac_antena"] == "AA:BB:CC:DD:EE:02" and filas[1]["id_plan_sugerido"] == 9
    assert "usuario_pppoe" not in filas[0]


def test_ip_fija_toma_las_colas_de_una_ip_y_la_mac_del_arp():
    colas = [
        {"name": "Ana", "target": "10.0.0.30/32", "max-limit": "5000000/10000000"},
        {"name": "Total", "target": "10.0.0.0/24", "max-limit": "100M/100M"},
        {"name": "Varias", "target": "10.0.0.31/32,10.0.0.32/32"},
        {"name": "Fuera", "target": "172.16.0.2/32"},
    ]
    arp = [{"address": "10.0.0.30", "mac-address": "AA:BB:CC:DD:EE:30"}]
    filas = imp.filas_estatica(colas, {**BASE, "id_olt": 4}, PLANES, 9, "10.0.0.0/24", arp)
    assert len(filas) == 1
    assert filas[0]["ip_asignada"] == "10.0.0.30" and filas[0]["id_plan_sugerido"] == 1
    assert filas[0]["mac_antena"] == "AA:BB:CC:DD:EE:30" and filas[0]["tipo_conexion"] == "fibra"


def test_pppoe_igual_que_antes():
    filas = imp.filas_pppoe([{"name": "juan", "password": "x", "profile": "Plus 30M", "remote-address": "10.0.0.5"}],
                            BASE, PLANES, 9)
    assert filas[0]["usuario_pppoe"] == "juan" and filas[0]["id_plan_sugerido"] == 2


def test_al_importar_dhcp_no_inventa_pppoe_y_pide_mac():
    datos, error = imp.acceso_de_fila("dhcp", {"ip_asignada": "10.0.0.20", "mac_antena": "aa-bb-cc-dd-ee-01",
                                               "tipo_conexion": "antena", "ip_antena": "10.5.0.20",
                                               "usuario_pppoe": "sobra"})
    assert error is None
    assert datos == {"user_pppoe": None, "pass_pppoe": None, "mac_address": "AA:BB:CC:DD:EE:01",
                     "tipo_conexion": "radio", "ip_antena": "10.5.0.20"}
    assert imp.acceso_de_fila("dhcp", {"ip_asignada": "10.0.0.20"})[1] == "falta la MAC del equipo (mac_antena)"
    assert "inválida" in imp.acceso_de_fila("dhcp", {"mac_antena": "zz"})[1]


def test_al_importar_ip_fija_basta_la_ip():
    datos, error = imp.acceso_de_fila("estatica", {"ip_asignada": "10.0.0.30", "tipo_conexion": "fibra"})
    assert error is None and datos["user_pppoe"] is None and datos["tipo_conexion"] == "fibra"
    assert imp.acceso_de_fila("estatica", {})[1] == "falta la IP (ip_asignada)"


def test_al_importar_pppoe_conserva_usuario():
    datos, _ = imp.acceso_de_fila("pppoe", {"usuario_pppoe": "juan", "password_pppoe": "1234",
                                            "tipo_conexion": "antena"})
    assert datos["user_pppoe"] == "juan" and datos["pass_pppoe"] == "1234" and datos["tipo_conexion"] == "fibra"
