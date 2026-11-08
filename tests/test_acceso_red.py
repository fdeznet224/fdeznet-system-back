"""Modo de acceso por cliente (PPPoE, DHCP, IP fija) y la cola de la IP fija."""
import asyncio
from types import SimpleNamespace

import pytest

from src.application.services import acceso_red
from src.application.services.mikrotik_reconciliation_service import MikrotikReconciliationService
from src.infrastructure.mikrotik_service import MikroTikService
from src.utils.mikrotik import formatear_cola_estatica, mismos_limites

PLAN = SimpleNamespace(nombre="20 Megas", velocidad_subida=5120, velocidad_bajada=20480,
                       burst_subida=0, burst_bajada=0, burst_time=8)


def _router(modo, **extra):
    return SimpleNamespace(id=1, nombre="Nodo", tipo_seguridad=modo, ip_vpn="192.0.2.1", user_api="api",
                           pass_api="x", port_api=80, is_active=True, **extra)


def test_el_modo_es_el_del_router():
    assert acceso_red.modo_acceso(SimpleNamespace(), _router("dhcp")) == "dhcp"
    assert acceso_red.modo_acceso(SimpleNamespace(router=_router("estatica"))) == "estatica"
    assert acceso_red.modo_acceso(SimpleNamespace(), None) == "pppoe"
    assert acceso_red.modo_router(_router("otro")) == "pppoe"


def test_faltantes_por_modo():
    sin_nada = SimpleNamespace(ip_asignada=None, mac_address=None, user_pppoe=None, pass_pppoe=None)
    assert acceso_red.faltantes("estatica", sin_nada) == ["IP"]
    assert acceso_red.faltantes("dhcp", sin_nada) == ["MAC WAN/CPE", "IP"]
    assert "usuario PPPoE" in acceso_red.faltantes("pppoe", sin_nada)
    # La IP fija no pide ni usuario ni MAC.
    assert acceso_red.faltantes("estatica", SimpleNamespace(ip_asignada="10.0.0.9")) == []


def test_limites_de_la_cola():
    assert formatear_cola_estatica(PLAN) == {"max-limit": "5M/20M"}
    con_rafaga = SimpleNamespace(**{**PLAN.__dict__, "burst_subida": 10240, "burst_bajada": 40960})
    assert formatear_cola_estatica(con_rafaga) == {
        "max-limit": "5M/20M", "burst-limit": "10M/40M", "burst-threshold": "5M/20M", "burst-time": "8/8"}
    # MikroTik devuelve números.
    assert mismos_limites("5000000/20000000", "5M/20M")
    assert not mismos_limites("5000000/10000000", "5M/20M")


class _MikroTikColas(MikroTikService):
    """Cola simple de verdad contra un /queue/simple en memoria."""

    def __init__(self):
        self.colas = []
        self.pedidos = []

    def _request(self, method, endpoint, payload=None, raise_on_error=False):
        self.pedidos.append((method, endpoint, payload))
        if method == "GET" and endpoint == "/queue/simple":
            return [dict(c) for c in self.colas]
        if method == "PUT" and endpoint == "/queue/simple":
            self.colas.append({".id": f"*{len(self.colas) + 1}", **payload})
            return self.colas[-1]
        if method == "PATCH":
            cola = next(c for c in self.colas if endpoint.endswith(c[".id"]))
            cola.update(payload)
            return cola
        if method == "DELETE":
            self.colas = [c for c in self.colas if not endpoint.endswith(c[".id"])]
            return True
        return None


def test_cola_de_ip_fija_se_crea_corrige_y_borra():
    mk = _MikroTikColas()
    cliente = SimpleNamespace(ip_asignada="10.0.0.9", mac_address=None, user_pppoe=None, pass_pppoe=None)
    acceso_red.aplicar(mk, "estatica", cliente, PLAN, "Juan | Servicio:1")
    assert mk.colas[0]["target"] == "10.0.0.9/32" and mk.colas[0]["max-limit"] == "5M/20M"
    assert mk.colas[0]["name"] == "fdeznet-10.0.0.9"
    # Otra vez: se corrige la misma, no se duplica.
    acceso_red.aplicar(mk, "estatica", cliente, SimpleNamespace(**{**PLAN.__dict__, "velocidad_bajada": 51200}), "Juan")
    assert len(mk.colas) == 1 and mk.colas[0]["max-limit"] == "5M/50M"
    assert acceso_red.bloquear(mk, "estatica", cliente, True) is True
    assert acceso_red.eliminar(mk, "estatica", cliente) is True and mk.colas == []
    with pytest.raises(ValueError):
        acceso_red.aplicar(mk, "estatica", SimpleNamespace(ip_asignada=None), PLAN, "x")


class _MikroTikIpFija:
    """Router de IP fija: solo colas."""

    def __init__(self):
        self.secrets = {}
        self.leases = {}
        self.colas = {}
        self.cortadas = set()
        self.leidos = []

    def inicializar_firewall_corte(self, solo_whatsapp=False, kbps=128):
        return True, "ok"

    def obtener_todos_pppoe_estricto(self):
        self.leidos.append("pppoe")
        return list(self.secrets.values())

    def obtener_todos_dhcp_estricto(self):
        self.leidos.append("dhcp")
        return list(self.leases.values())

    def obtener_todas_colas_estricto(self):
        self.leidos.append("colas")
        return list(self.colas.values())

    def obtener_ips_cortadas(self):
        return set(self.cortadas)

    def obtener_pppoe_estricto(self, usuario):
        return self.secrets.get(usuario)

    def crear_actualizar_cola_estatica(self, ip, limites, comment):
        self.colas[ip] = {"target": f"{ip}/32", "max-limit": "5000000/20000000", "disabled": "false"}
        return self.colas[ip]

    def obtener_cola_estatica_estricto(self, ip):
        return self.colas.get(ip)

    def gestionar_corte_cliente(self, ip, suspender):
        (self.cortadas.add if suspender else self.cortadas.discard)(ip)
        return True


class _DB:
    def __init__(self, servicios):
        self.servicios, self.logs = servicios, []

    async def get(self, _modelo, _id):
        return None

    async def execute(self, _consulta):
        servicios = self.servicios

        class _R:
            def scalars(self):
                return self

            def unique(self):
                return self

            def all(self):
                return servicios
        return _R()

    def add(self, valor):
        self.logs.append(valor)

    async def commit(self):
        pass


def test_conciliador_crea_la_cola_y_corta_la_ip_fija():
    router = _router("estatica")
    base = dict(router_id=1, router=router, plan=PLAN, cliente=SimpleNamespace(nombre="X"), is_online=True)
    activo = SimpleNamespace(id=1, cliente_id=1, alias="Casa", estado="activo",
                             user_pppoe=None, pass_pppoe=None, ip_asignada="10.0.0.2", mac_address=None, **base)
    fija = SimpleNamespace(id=2, cliente_id=2, alias="Tienda", estado="suspendido",
                           user_pppoe=None, pass_pppoe=None, ip_asignada="10.0.0.9", mac_address=None, **base)
    mk = _MikroTikIpFija()
    mk.colas["10.0.0.2"] = {"target": "10.0.0.2/32", "max-limit": "5000000/20000000", "disabled": "false"}

    async def directo(funcion, *args):
        return funcion(*args)

    servicio = MikrotikReconciliationService(_DB([activo, fija]), mikrotik_factory=lambda *a: mk,
                                             blocking_runner=directo)
    reporte = asyncio.run(servicio.ejecutar())
    assert reporte["errores"] == 0, [l.mensaje for l in servicio.db.logs]
    assert reporte["reparados"] == 1 and reporte["correctos"] == 1
    # Solo se leen las colas: ni secrets PPPoE ni leases.
    assert mk.leidos == ["colas"]
    assert "10.0.0.9" in mk.colas and mk.cortadas == {"10.0.0.9"}
    assert fija.is_online is False
