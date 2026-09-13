import asyncio
from pathlib import Path
from types import SimpleNamespace

from fastapi.routing import APIRoute

from src.application.services.mikrotik_reconciliation_service import (
    MikrotikReconciliationService,
)
from src.infrastructure.models import LogCronjobModel
from src.main import app


class _ScalarResult:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return self

    def unique(self):
        return self

    def all(self):
        return self.values


class _DB:
    def __init__(self, servicios):
        self.servicios = servicios
        self.logs = []
        self.commits = 0

    async def execute(self, _statement):
        return _ScalarResult(self.servicios)

    def add(self, value):
        if isinstance(value, LogCronjobModel):
            self.logs.append(value)

    async def commit(self):
        self.commits += 1


class _FakeMikrotik:
    def __init__(self, secrets, ips_cortadas):
        self.secrets = {
            item["name"]: dict(item)
            for item in secrets
        }
        self.ips_cortadas = set(ips_cortadas)
        self.listados = 0
        self.cambios_estado = []
        self.cambios_corte = []
        self.configurados = []
        self.leases = {}

    def obtener_todos_pppoe_estricto(self):
        self.listados += 1
        return list(self.secrets.values())

    def obtener_ips_cortadas(self):
        return set(self.ips_cortadas)

    def obtener_pppoe_estricto(self, usuario):
        return self.secrets.get(usuario)

    def crear_actualizar_pppoe(
        self,
        user,
        password,
        profile,
        remote_address,
        comment,
    ):
        self.configurados.append(user)
        self.secrets[user] = {
            "name": user,
            "password": password,
            "profile": profile,
            "remote-address": remote_address,
            "disabled": "false",
            "comment": comment,
        }
        return True

    def activar_desactivar_pppoe(self, usuario, disabled):
        self.cambios_estado.append((usuario, disabled))
        self.secrets[usuario]["disabled"] = (
            "true" if disabled else "false"
        )
        return True

    def gestionar_corte_cliente(self, ip, suspender):
        self.cambios_corte.append((ip, suspender))
        if suspender:
            self.ips_cortadas.add(ip)
        else:
            self.ips_cortadas.discard(ip)
        return True

    def obtener_todos_dhcp_estricto(self):
        self.listados += 1
        return list(self.leases.values())

    def obtener_lease_dhcp_estricto(self, mac):
        return self.leases.get(mac)

    def crear_actualizar_lease_dhcp(
        self, mac, address, rate_limit, comment
    ):
        self.configurados.append(mac)
        self.leases[mac] = {
            ".id": f"*{len(self.leases) + 1}",
            "mac-address": mac,
            "address": address,
            "rate-limit": rate_limit,
            "block-access": "false",
            "comment": comment,
        }
        return self.leases[mac]

    def activar_desactivar_dhcp(self, mac, blocked):
        self.cambios_estado.append((mac, blocked))
        self.leases[mac]["block-access"] = (
            "true" if blocked else "false"
        )
        return True


async def _run_direct(function, *args):
    return function(*args)


def _servicio(servicio_id, estado, router, usuario, ip):
    return SimpleNamespace(
        id=servicio_id,
        cliente_id=20 + servicio_id,
        alias=f"Casa {servicio_id}",
        estado=estado,
        router_id=router.id,
        router=router,
        plan=SimpleNamespace(nombre="50 Megas"),
        cliente=SimpleNamespace(nombre=f"Cliente {servicio_id}"),
        user_pppoe=usuario,
        pass_pppoe="secreto",
        ip_asignada=ip,
        is_online=True,
        mac_address=None,
    )


def test_conciliador_repara_activo_bloqueado_y_suspendido_habilitado():
    router = SimpleNamespace(
        id=1,
        nombre="Nodo",
        ip_vpn="192.0.2.1",
        user_api="api",
        pass_api="clave",
        port_api=80,
        is_active=True,
    )
    activo = _servicio(1, "activo", router, "activo1", "10.0.0.1")
    suspendido = _servicio(
        2,
        "suspendido",
        router,
        "suspendido2",
        "10.0.0.2",
    )
    mk = _FakeMikrotik(
        secrets=[
            {
                "name": "activo1",
                "password": "secreto",
                "profile": "50 Megas",
                "remote-address": "10.0.0.1",
                "disabled": "true",
            },
            {
                "name": "suspendido2",
                "password": "secreto",
                "profile": "50 Megas",
                "remote-address": "10.0.0.2",
                "disabled": "false",
            },
        ],
        ips_cortadas={"10.0.0.1"},
    )
    db = _DB([activo, suspendido])

    reporte = asyncio.run(
        MikrotikReconciliationService(
            db,
            mikrotik_factory=lambda *_args: mk,
            blocking_runner=_run_direct,
        ).ejecutar()
    )

    assert reporte == {
        "verificados": 2,
        "correctos": 0,
        "reparados": 2,
        "errores": 0,
        "routers": 1,
    }
    assert ("activo1", False) in mk.cambios_estado
    assert ("suspendido2", True) in mk.cambios_estado
    assert ("10.0.0.1", False) in mk.cambios_corte
    assert ("10.0.0.2", True) in mk.cambios_corte
    assert suspendido.is_online is False
    assert sum(log.nivel == "WARNING" for log in db.logs) == 2
    assert db.commits == 1


def test_conciliador_recrea_secret_faltante():
    router = SimpleNamespace(
        id=1,
        nombre="Nodo",
        ip_vpn="192.0.2.1",
        user_api="api",
        pass_api="clave",
        port_api=80,
        is_active=True,
    )
    servicio = _servicio(
        5,
        "activo",
        router,
        "faltante5",
        "10.0.0.5",
    )
    mk = _FakeMikrotik([], set())
    db = _DB([servicio])

    reporte = asyncio.run(
        MikrotikReconciliationService(
            db,
            mikrotik_factory=lambda *_args: mk,
            blocking_runner=_run_direct,
        ).ejecutar()
    )

    assert reporte["reparados"] == 1
    assert mk.configurados == ["faltante5"]
    assert mk.secrets["faltante5"]["remote-address"] == "10.0.0.5"


def test_conciliador_registra_error_reintentable():
    router = SimpleNamespace(
        id=1,
        nombre="Nodo",
        ip_vpn="192.0.2.1",
        user_api="api",
        pass_api="clave",
        port_api=80,
        is_active=True,
    )
    servicio = _servicio(
        7,
        "activo",
        router,
        "incompleto7",
        "10.0.0.7",
    )
    servicio.pass_pppoe = None
    db = _DB([servicio])
    mk = _FakeMikrotik([], set())

    reporte = asyncio.run(
        MikrotikReconciliationService(
            db,
            mikrotik_factory=lambda *_args: mk,
            blocking_runner=_run_direct,
        ).ejecutar()
    )

    assert reporte["errores"] == 1
    assert any(
        "se reintentará" in log.mensaje
        and "contraseña PPPoE" in log.mensaje
        for log in db.logs
    )


def test_conciliador_no_sobrescribe_usuarios_pppoe_repetidos():
    router = SimpleNamespace(
        id=1,
        nombre="Nodo",
        ip_vpn="192.0.2.1",
        user_api="api",
        pass_api="clave",
        port_api=80,
        is_active=True,
    )
    primero = _servicio(
        8,
        "activo",
        router,
        "repetido",
        "10.0.0.8",
    )
    segundo = _servicio(
        9,
        "activo",
        router,
        "repetido",
        "10.0.0.9",
    )
    db = _DB([primero, segundo])
    mk = _FakeMikrotik([], set())

    reporte = asyncio.run(
        MikrotikReconciliationService(
            db,
            mikrotik_factory=lambda *_args: mk,
            blocking_runner=_run_direct,
        ).ejecutar()
    )

    assert reporte["errores"] == 2
    assert reporte["routers"] == 0
    assert mk.configurados == []
    assert sum(
        "usuario PPPoE está repetido" in log.mensaje
        for log in db.logs
    ) == 2


def test_conciliador_dhcp_repara_rate_limit_y_bloqueo():
    router = SimpleNamespace(
        id=2,
        nombre="Nodo DHCP",
        ip_vpn="192.0.2.2",
        user_api="api",
        pass_api="clave",
        port_api=80,
        is_active=True,
        tipo_seguridad="dhcp",
    )
    servicio = _servicio(
        10, "suspendido", router, None, "10.0.1.10"
    )
    servicio.mac_address = "aa-bb-cc-dd-ee-10"
    servicio.pass_pppoe = None
    servicio.plan = SimpleNamespace(
        nombre="50 Megas",
        velocidad_subida=10240,
        velocidad_bajada=51200,
        burst_subida=0,
        burst_bajada=0,
        burst_time=0,
    )
    mk = _FakeMikrotik([], set())
    db = _DB([servicio])

    reporte = asyncio.run(
        MikrotikReconciliationService(
            db,
            mikrotik_factory=lambda *_args: mk,
            blocking_runner=_run_direct,
        ).ejecutar()
    )

    mac = "AA:BB:CC:DD:EE:10"
    assert reporte["reparados"] == 1
    assert mk.leases[mac]["address"] == "10.0.1.10"
    assert mk.leases[mac]["rate-limit"] == "10M/50M"
    assert mk.leases[mac]["block-access"] == "true"
    assert ("10.0.1.10", True) in mk.cambios_corte


def test_ruta_y_cron_de_conciliacion_estan_publicados():
    rutas = {
        (route.path, method)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    assert ("/network/conciliar-mikrotik", "POST") in rutas

    main_source = (
        Path(__file__).parents[1] / "src" / "main.py"
    ).read_text(encoding="utf-8")
    assert 'id="mikrotik_state_reconciler"' in main_source
    assert "minutes=5" in main_source
