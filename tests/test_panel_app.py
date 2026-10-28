"""Panel central: sesión, CSRF, alta, pagos y suspensión (Docker simulado)."""
import importlib
import re
from datetime import date

import pytest
from fastapi.testclient import TestClient
from passlib.hash import pbkdf2_sha256


@pytest.fixture
def panel(tmp_path, monkeypatch):
    monkeypatch.setenv("PANEL_CLAVE_HASH", pbkdf2_sha256.hash("clave-del-dueno"))
    monkeypatch.setenv("PANEL_SECRETO", "s" * 64)
    monkeypatch.setenv("PANEL_BASE", str(tmp_path / "panel.db"))
    monkeypatch.setenv("PANEL_DOMINIO_BASE", "ejemplo.com")
    monkeypatch.setenv("PANEL_COOKIE_SEGURA", "0")
    from nube.panel import db, app as app_mod, docker_ops

    importlib.reload(db)
    app_mod = importlib.reload(app_mod)
    llamadas = []
    monkeypatch.setattr(docker_ops, "crear", lambda slug, *a, **k: llamadas.append(("crear", slug, a, k)) or "ClaveInicial123")
    monkeypatch.setattr(docker_ops, "detener", lambda slug: llamadas.append(("detener", slug)))
    monkeypatch.setattr(docker_ops, "encender", lambda slug: llamadas.append(("encender", slug)))
    monkeypatch.setattr(docker_ops, "aplicar_ajustes", lambda slug, **k: llamadas.append(("ajustes", slug, k)))
    monkeypatch.setattr(docker_ops, "estado_contenedores", lambda: {})
    monkeypatch.setattr(docker_ops, "uso_memoria", lambda: {})
    monkeypatch.setattr(docker_ops, "versiones_disponibles", lambda: ["2.109.0"])
    monkeypatch.setattr(docker_ops, "puertos_en_uso", lambda: ([], []))
    cliente = TestClient(app_mod.app, follow_redirects=False)
    return cliente, llamadas


def _entrar(cliente):
    respuesta = cliente.post("/entrar", data={"usuario": "dueno", "clave": "clave-del-dueno"})
    assert respuesta.status_code == 303
    pagina = cliente.get("/").text
    return re.search(r'name="csrf" value="([0-9a-f]+)"', pagina).group(1)


def test_sin_sesion_manda_a_entrar(panel):
    cliente, _ = panel
    respuesta = cliente.get("/")
    assert respuesta.status_code == 303
    assert respuesta.headers["location"] == "/entrar"


def test_clave_incorrecta_y_bloqueo_por_intentos(panel):
    cliente, _ = panel
    for _ in range(5):
        assert "incorrectos" in cliente.post("/entrar", data={"usuario": "dueno", "clave": "mala"}).text
    bloqueado = cliente.post("/entrar", data={"usuario": "dueno", "clave": "clave-del-dueno"})
    assert "Demasiados intentos" in bloqueado.text


def test_formulario_sin_csrf_se_rechaza(panel):
    cliente, llamadas = panel
    _entrar(cliente)
    respuesta = cliente.post("/isp/nuevo", data={"nombre": "Colega", "slug": "colega"})
    assert respuesta.status_code == 403
    assert llamadas == []


def test_alta_pago_y_suspension(panel):
    cliente, llamadas = panel
    csrf = _entrar(cliente)

    alta = cliente.post("/isp/nuevo", data={
        "csrf": csrf, "nombre": "Red Colega", "slug": "colega", "precio_mensual": "800",
        "dia_pago": "10", "limite_clientes": "300", "whatsapp": "true", "version": "2.109.0",
    })
    assert alta.status_code == 200
    assert "ClaveInicial123" in alta.text
    assert "colega.ejemplo.com" in alta.text
    _, slug, puertos, opciones = llamadas[0]
    assert (slug, puertos[:2]) == ("colega", (18001, 51821))
    assert opciones["limite_clientes"] == 300 and opciones["whatsapp"] is True

    pago = cliente.post("/isp/1/pago", data={
        "csrf": csrf, "monto": "1600", "meses": "2", "fecha": "2026-10-28",
    })
    assert pago.status_code == 303
    detalle = cliente.get("/isp/1").text
    assert "10/12/2026" in detalle  # dos meses al día 10
    assert "ClaveInicial123" not in detalle

    assert cliente.post("/isp/1/suspender", data={"csrf": csrf}).status_code == 303
    assert ("detener", "colega") in llamadas
    assert "Reactivar ISP" in cliente.get("/isp/1").text


def test_segundo_isp_toma_los_siguientes_puertos(panel):
    cliente, llamadas = panel
    csrf = _entrar(cliente)
    for slug in ("uno", "dos"):
        cliente.post("/isp/nuevo", data={"csrf": csrf, "nombre": slug, "slug": slug})
    assert [c[2][:2] for c in llamadas] == [(18001, 51821), (18002, 51822)]


def test_borrar_ultimo_pago_regresa_la_fecha(panel):
    cliente, _ = panel
    csrf = _entrar(cliente)
    cliente.post("/isp/nuevo", data={"csrf": csrf, "nombre": "A", "slug": "aaa", "dia_pago": "5"})
    cliente.post("/isp/1/pago", data={"csrf": csrf, "monto": "500", "meses": "1", "fecha": "2026-10-28"})
    cliente.post("/isp/1/pago", data={"csrf": csrf, "monto": "500", "meses": "1", "fecha": "2026-10-28"})
    from nube.panel.db import Isp, Pago, Sesion

    with Sesion() as db:
        assert db.get(Isp, 1).pagado_hasta == date(2026, 12, 5)
        ultimo = max(p.id for p in db.query(Pago).all())
    cliente.post(f"/isp/1/pago/{ultimo}/borrar", data={"csrf": csrf})
    with Sesion() as db:
        assert db.get(Isp, 1).pagado_hasta == date(2026, 11, 5)


def test_alta_salta_puertos_de_instancias_hechas_a_mano(panel, monkeypatch):
    cliente, llamadas = panel
    from nube.panel import docker_ops

    monkeypatch.setattr(docker_ops, "puertos_en_uso", lambda: ([18001], [51821]))
    csrf = _entrar(cliente)
    cliente.post("/isp/nuevo", data={"csrf": csrf, "nombre": "Demo", "slug": "demo"})
    assert llamadas[0][2][:2] == (18002, 51822)
