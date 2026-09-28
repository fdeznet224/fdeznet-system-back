import asyncio
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlsplit

import pytest
from pydantic import ValidationError

from src.application.services.corte_whatsapp_service import (
    ModoCorte,
    obtener_modo_corte,
)
from src.domain.schemas import SystemConfigUpdate
from src.infrastructure.mikrotik_service import MikroTikService
from src.utils.mikrotik import RANGOS_META_WHATSAPP, debe_bloquear_acceso


class _RouterOSFalso(MikroTikService):
    """Simula la API REST de RouterOS con tablas ordenadas en memoria."""

    def __init__(self, tablas=None):
        self.tablas = {
            ruta: [dict(item) for item in items]
            for ruta, items in (tablas or {}).items()
        }
        self.escrituras = []
        self.scripts = []
        self._siguiente = 100

    def _nuevo_id(self):
        self._siguiente += 1
        return f"*{self._siguiente:X}"

    def _request(self, method, endpoint, payload=None, raise_on_error=False):
        partes = urlsplit(endpoint)
        ruta = partes.path
        filtros = dict(parse_qsl(partes.query))
        if method == "GET":
            return [
                dict(item)
                for item in self.tablas.get(ruta, [])
                if all(str(item.get(k)) == v for k, v in filtros.items())
            ]

        self.escrituras.append((method, endpoint, payload))
        if method == "POST" and ruta == "/execute":
            self.scripts.append(payload["script"])
            return []
        if method == "PUT":
            item = {".id": self._nuevo_id(), "disabled": "false", **payload}
            self.tablas.setdefault(ruta, []).append(item)
            return dict(item)
        if method == "POST" and ruta.endswith("/move"):
            tabla = self.tablas[ruta.rsplit("/", 1)[0]]
            item = next(i for i in tabla if i[".id"] == payload["numbers"])
            tabla.remove(item)
            destino = next(
                pos for pos, i in enumerate(tabla)
                if i[".id"] == payload["destination"]
            )
            tabla.insert(destino, item)
            return []
        base, item_id = ruta.rsplit("/", 1)
        tabla = self.tablas[base]
        item = next(i for i in tabla if i[".id"] == item_id)
        if method == "PATCH":
            item.update(payload)
            return dict(item)
        if method == "DELETE":
            tabla.remove(item)
            return True
        raise AssertionError((method, endpoint, payload))


def _router_con_reglas_previas():
    return _RouterOSFalso(
        {
            "/ip/firewall/filter": [
                {
                    ".id": "*0", "chain": "forward", "dynamic": "true",
                    "action": "passthrough",
                    "comment": "special dummy rule to show fasttrack counters",
                },
                {
                    ".id": "*1", "chain": "input", "action": "accept",
                    "comment": "input del operador",
                },
                {
                    ".id": "*2", "chain": "forward",
                    "action": "fasttrack-connection",
                    "connection-state": "established,related",
                },
                {
                    ".id": "*3", "chain": "forward", "action": "accept",
                    "connection-state": "established,related",
                },
            ],
        }
    )


def _comentarios_forward(mk, ruta):
    return [
        regla.get("comment") or regla[".id"]
        for regla in mk.tablas.get(ruta, [])
        if regla.get("chain") == "forward"
        and regla.get("dynamic") != "true"
    ]


def test_modo_whatsapp_crea_reglas_antes_de_fasttrack():
    mk = _router_con_reglas_previas()

    ok, mensaje = mk.inicializar_firewall_corte(solo_whatsapp=True, kbps=96)

    assert ok, mensaje
    assert _comentarios_forward(mk, "/ip/firewall/filter") == [
        "=== CORTE WHATSAPP SALIDA ===",
        "=== CORTE WHATSAPP ENTRADA ===",
        "=== CORTE DNS UDP ===",
        "=== CORTE DNS TCP ===",
        "*2",
        "*3",
        "=== BLOQUEO MOROSOS ===",
    ]
    assert _comentarios_forward(mk, "/ip/firewall/mangle") == [
        "=== CORTE WHATSAPP MARCA SUBIDA ===",
        "=== CORTE WHATSAPP MARCA BAJADA ===",
    ]
    lista = {
        item["address"]
        for item in mk.tablas["/ip/firewall/address-list"]
        if item["list"] == "WHATSAPP_FDEZNET"
    }
    assert lista == set(RANGOS_META_WHATSAPP)
    tipos = {t["name"]: t for t in mk.tablas["/queue/type"]}
    assert tipos["fdeznet-corte-wa-subida"]["pcq-rate"] == "96k"
    assert tipos["fdeznet-corte-wa-subida"]["pcq-classifier"] == "src-address"
    assert tipos["fdeznet-corte-wa-bajada"]["pcq-classifier"] == "dst-address"
    arboles = {t["name"]: t for t in mk.tablas["/queue/tree"]}
    assert arboles["FDEZNET-CORTE-WA-BAJADA"]["packet-mark"] == (
        "fdeznet-corte-wa-bajada"
    )
    assert arboles["FDEZNET-CORTE-WA-BAJADA"]["parent"] == "global"


def test_modo_whatsapp_es_idempotente():
    mk = _router_con_reglas_previas()
    mk.inicializar_firewall_corte(solo_whatsapp=True, kbps=128)
    mk.escrituras.clear()

    ok, _ = mk.inicializar_firewall_corte(solo_whatsapp=True, kbps=128)

    assert ok
    assert mk.escrituras == []


def test_cambiar_velocidad_solo_actualiza_la_cola():
    mk = _router_con_reglas_previas()
    mk.inicializar_firewall_corte(solo_whatsapp=True, kbps=128)
    mk.escrituras.clear()

    mk.inicializar_firewall_corte(solo_whatsapp=True, kbps=256)

    assert [m for m, *_ in mk.escrituras] == ["PATCH", "PATCH"]
    assert {t["pcq-rate"] for t in mk.tablas["/queue/type"]} == {"256k"}


def test_reordena_reglas_si_alguien_las_movio():
    mk = _router_con_reglas_previas()
    mk.inicializar_firewall_corte(solo_whatsapp=True)
    filtro = mk.tablas["/ip/firewall/filter"]
    salida = next(
        r for r in filtro
        if r.get("comment") == "=== CORTE WHATSAPP SALIDA ==="
    )
    filtro.remove(salida)
    filtro.append(salida)

    mk.inicializar_firewall_corte(solo_whatsapp=True)

    assert _comentarios_forward(mk, "/ip/firewall/filter")[:4] == [
        "=== CORTE WHATSAPP SALIDA ===",
        "=== CORTE WHATSAPP ENTRADA ===",
        "=== CORTE DNS UDP ===",
        "=== CORTE DNS TCP ===",
    ]


def test_desactivar_modo_whatsapp_limpia_todo_y_conserva_bloqueo():
    mk = _router_con_reglas_previas()
    mk.inicializar_firewall_corte(solo_whatsapp=True)

    ok, _ = mk.inicializar_firewall_corte(solo_whatsapp=False)

    assert ok
    assert _comentarios_forward(mk, "/ip/firewall/filter") == [
        "*2", "*3", "=== BLOQUEO MOROSOS ===",
    ]
    assert mk.tablas["/ip/firewall/mangle"] == []
    assert mk.tablas["/queue/tree"] == []
    assert mk.tablas["/queue/type"] == []
    assert mk.tablas["/ip/firewall/address-list"] == []


def test_corte_total_no_toca_nada_de_whatsapp():
    mk = _router_con_reglas_previas()

    ok, _ = mk.inicializar_firewall_corte()

    assert ok
    assert [m for m, *_ in mk.escrituras] == ["PUT"]


def test_suspender_cierra_conexiones_abiertas_del_cliente():
    mk = _RouterOSFalso({"/ip/firewall/address-list": []})

    assert mk.gestionar_corte_cliente("10.0.0.5", suspender=True) is True

    assert mk.scripts == [
        "/ip firewall connection remove [find where "
        'src-address~"^10.0.0.5:" or dst-address~"^10.0.0.5:"]'
    ]


def test_limpiar_conexiones_rechaza_ip_invalida():
    mk = _RouterOSFalso()

    assert mk.limpiar_conexiones_cliente('10.0.0.5"; /system reboot') is False
    assert mk.scripts == []


@pytest.mark.parametrize(
    "suspendido, solo_whatsapp, esperado",
    [
        (False, False, False),
        (False, True, False),
        (True, False, True),
        (True, True, False),
    ],
)
def test_debe_bloquear_acceso(suspendido, solo_whatsapp, esperado):
    assert debe_bloquear_acceso(suspendido, solo_whatsapp) is esperado


class _DBConfig:
    def __init__(self, config=None, error=None):
        self.config = config
        self.error = error

    async def get(self, _model, _id):
        if self.error:
            raise self.error
        return self.config


def test_obtener_modo_corte_lee_configuracion():
    db = _DBConfig(
        SimpleNamespace(corte_solo_whatsapp=True, corte_whatsapp_kbps=64)
    )

    assert asyncio.run(obtener_modo_corte(db)) == ModoCorte(True, 64)


def test_obtener_modo_corte_usa_corte_total_ante_dudas():
    assert asyncio.run(obtener_modo_corte(_DBConfig())) == ModoCorte()
    assert asyncio.run(
        obtener_modo_corte(_DBConfig(error=RuntimeError("sin tabla")))
    ) == ModoCorte()


def test_esquema_limita_velocidad_whatsapp():
    base = dict(
        activar_corte_automatico=True,
        hora_ejecucion_corte="03:00",
        recordatorio_1_dias=5,
        recordatorio_2_dias=1,
        recordatorio_3_dias=0,
        activar_notificaciones=True,
        generar_facturas_automaticamente=True,
        aviso_pantalla_corte=False,
    )
    assert SystemConfigUpdate(**base).corte_solo_whatsapp is False
    assert SystemConfigUpdate(**base).corte_whatsapp_kbps == 128
    with pytest.raises(ValidationError):
        SystemConfigUpdate(**base, corte_whatsapp_kbps=8)
    with pytest.raises(ValidationError):
        SystemConfigUpdate(**base, corte_whatsapp_kbps=100000)
