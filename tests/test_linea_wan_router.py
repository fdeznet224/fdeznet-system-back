"""Detección de la línea de internet principal o de respaldo del MikroTik."""
from src.application.services.router_monitor_service import (
    evaluar_wan,
    mensaje_cambio_wan,
)


def ruta(gateway, distancia, activa, deshabilitada=False, **extra):
    datos = {
        "dst-address": "0.0.0.0/0",
        "gateway": gateway,
        "distance": str(distancia),
        "routing-table": "main",
        "disabled": "true" if deshabilitada else "false",
    }
    if activa:
        datos["active"] = "true"
    datos.update(extra)
    return datos


def test_sale_por_la_principal():
    lectura = evaluar_wan([
        ruta("200.1.1.1", 1, True, comment="TELMEX DEDICADO", **{"immediate-gw": "200.1.1.1%ether1"}),
        ruta("192.168.8.1", 2, False),
    ], None)
    assert lectura["estado"] == "principal"
    assert lectura["enlace"] == "TELMEX DEDICADO (200.1.1.1 por ether1)"
    assert lectura["principal"] == "200.1.1.1"


def test_detecta_respaldo_por_distancia():
    lectura = evaluar_wan([
        ruta("200.1.1.1", 1, False, comment="DEDICADO"),
        ruta("192.168.8.1", 2, True, comment="RESPALDO TELCEL"),
    ], "200.1.1.1")
    assert lectura["estado"] == "respaldo"
    assert lectura["enlace"].startswith("RESPALDO TELCEL")
    assert lectura["nombre_principal"].startswith("DEDICADO")


def test_respaldo_aunque_netwatch_deshabilite_la_principal():
    rutas = [
        ruta("200.1.1.1", 1, False, deshabilitada=True),
        ruta("192.168.8.1", 2, True),
    ]
    assert evaluar_wan(rutas, "200.1.1.1")["estado"] == "respaldo"
    # Sin recordar la principal, una ruta deshabilitada no cuenta.
    assert evaluar_wan(rutas, None)["estado"] == "principal"


def test_sin_ninguna_linea_activa():
    lectura = evaluar_wan([ruta("200.1.1.1", 1, False), ruta("192.168.8.1", 2, False)], None)
    assert lectura["estado"] == "sin_internet"


def test_ignora_tablas_de_balanceo_y_sin_rutas():
    rutas = [
        ruta("200.1.1.1", 1, True, **{"routing-table": "a_isp2"}),
        ruta("192.168.8.1", 1, True),
    ]
    assert evaluar_wan(rutas, None)["clave_activa"] == "192.168.8.1"
    assert evaluar_wan([], None) is None
    assert evaluar_wan(None, None) is None


def test_balanceo_misma_distancia_es_principal():
    lectura = evaluar_wan([ruta("200.1.1.1", 1, True), ruta("192.168.8.1", 1, True)], None)
    assert lectura["estado"] == "principal"


def test_mensajes():
    respaldo = {"estado": "respaldo", "enlace": "LTE", "nombre_principal": "DEDICADO"}
    principal = {"estado": "principal", "enlace": "DEDICADO", "nombre_principal": "DEDICADO"}
    assert "respaldo LTE" in mensaje_cambio_wan("Centro", "principal", respaldo, "hoy")
    assert "volvió a la línea principal" in mensaje_cambio_wan("Centro", "respaldo", principal, "hoy")
    assert mensaje_cambio_wan("Centro", None, principal, "hoy") is None
    assert mensaje_cambio_wan("Centro", "principal", principal, "hoy") is None


def test_tarea_avisa_tras_dos_lecturas(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from src import jobs

    avisos = []

    async def falso_aviso(mensaje, db, tipo_evento="alerta_router"):
        avisos.append((mensaje, tipo_evento))

    monkeypatch.setattr(jobs, "enviar_alertas_whatsapp", falso_aviso)
    jobs._wan_pendiente.clear()
    router = SimpleNamespace(id=99, nombre="Centro", wan_estado=None, wan_enlace=None, wan_principal=None, wan_desde=None)
    db = SimpleNamespace(add=lambda _x: None)
    rutas = {"valor": [ruta("200.1.1.1", 1, True), ruta("192.168.8.1", 2, False)]}
    mk = SimpleNamespace(rutas_por_defecto=lambda: rutas["valor"])

    asyncio.run(jobs._revisar_linea_wan(router, mk, db))
    assert router.wan_estado == "principal" and avisos == []

    rutas["valor"] = [ruta("200.1.1.1", 1, False), ruta("192.168.8.1", 2, True)]
    asyncio.run(jobs._revisar_linea_wan(router, mk, db))
    assert router.wan_estado == "principal" and avisos == []  # Un parpadeo no avisa.
    asyncio.run(jobs._revisar_linea_wan(router, mk, db))
    assert router.wan_estado == "respaldo"
    assert len(avisos) == 1 and avisos[0][1] == "alerta_internet"

    rutas["valor"] = [ruta("200.1.1.1", 1, True), ruta("192.168.8.1", 2, False)]
    asyncio.run(jobs._revisar_linea_wan(router, mk, db))
    asyncio.run(jobs._revisar_linea_wan(router, mk, db))
    assert router.wan_estado == "principal"
    assert "volvió" in avisos[-1][0]
