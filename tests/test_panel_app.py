"""Panel central: sesión, CSRF, alta, pagos y suspensión (Docker simulado)."""
import importlib
import re
from datetime import date, datetime

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
    monkeypatch.setattr(docker_ops, "restablecer_admin", lambda slug, usuario, clave: llamadas.append(("restablecer", slug, usuario, clave)))
    monkeypatch.setattr(docker_ops, "respaldar", lambda slug, destino=None: llamadas.append(("respaldar", slug)) or {
        "ARCHIVO": f"{slug}-20261028-030000.tar", "TAMANO": "2048", "REMOTO": "ok"})
    monkeypatch.setattr(docker_ops, "restaurar", lambda slug, nombre, **k: llamadas.append(("restaurar", slug, nombre, k)))
    monkeypatch.setattr(docker_ops, "listar_respaldos", lambda slug: [
        {"nombre": f"{slug}-20261028-030000.tar", "tamano": 2048, "fecha": datetime(2026, 10, 28, 3)}])
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


def test_respaldar_ahora_y_ver_su_estado(panel):
    cliente, llamadas = panel
    csrf = _entrar(cliente)
    cliente.post("/isp/nuevo", data={"csrf": csrf, "nombre": "A", "slug": "aaa"})
    assert "Todavía sin respaldo" in cliente.get("/isp/1").text

    assert cliente.post("/isp/1/respaldar", data={"csrf": csrf}).status_code == 303
    assert ("respaldar", "aaa") in llamadas
    detalle = cliente.get("/isp/1").text
    assert "y fuera de la VPS" in detalle
    assert "aaa-20261028-030000.tar" in detalle


def test_respaldo_fallido_se_marca_en_inicio(panel, monkeypatch):
    cliente, _ = panel
    from nube.panel import docker_ops

    def falla(slug, destino=None):
        raise docker_ops.ErrorDocker("disco lleno")

    monkeypatch.setattr(docker_ops, "respaldar", falla)
    csrf = _entrar(cliente)
    cliente.post("/isp/nuevo", data={"csrf": csrf, "nombre": "A", "slug": "aaa"})
    respuesta = cliente.post("/isp/1/respaldar", data={"csrf": csrf})
    assert "disco" in respuesta.headers["location"]
    inicio = cliente.get("/").text
    assert "Respaldo: Falló" in inicio and "disco lleno" in inicio
    assert "respaldo falló" in cliente.get("/isp/1").text.lower()


def test_restaurar_pide_escribir_el_subdominio(panel):
    cliente, llamadas = panel
    csrf = _entrar(cliente)
    cliente.post("/isp/nuevo", data={"csrf": csrf, "nombre": "A", "slug": "aaa"})
    ruta = "/isp/1/respaldos/aaa-20261028-030000.tar/restaurar"

    cliente.post(ruta, data={"csrf": csrf, "confirmar": "otro"})
    assert not [c for c in llamadas if c[0] == "restaurar"]

    cliente.post("/isp/1/suspender", data={"csrf": csrf})
    cliente.post(ruta, data={"csrf": csrf, "confirmar": "aaa"})
    assert ("restaurar", "aaa", "aaa-20261028-030000.tar", {"arrancar": False}) in llamadas


def test_descarga_no_sale_de_la_carpeta_del_isp(panel, tmp_path, monkeypatch):
    cliente, _ = panel
    from nube.panel import docker_ops

    monkeypatch.setattr(docker_ops, "RESPALDOS", tmp_path)
    (tmp_path / "aaa").mkdir()
    (tmp_path / "aaa" / "aaa-20261028-030000.tar").write_bytes(b"respaldo")
    csrf = _entrar(cliente)
    cliente.post("/isp/nuevo", data={"csrf": csrf, "nombre": "A", "slug": "aaa"})

    assert cliente.get("/isp/1/respaldos/aaa-20261028-030000.tar").content == b"respaldo"
    assert cliente.get("/isp/1/respaldos/..%2Fpanel.db").status_code == 404
    assert cliente.get("/isp/1/respaldos/bbb-20261028-030000.tar").status_code == 404


def test_caddy_solo_saca_certificados_de_isp_dados_de_alta(panel):
    cliente, _ = panel
    csrf = _entrar(cliente)
    cliente.post("/isp/nuevo", data={"csrf": csrf, "nombre": "A", "slug": "aaa"})
    assert cliente.get("/caddy/permitir", params={"domain": "aaa.ejemplo.com"}).status_code == 200
    assert cliente.get("/caddy/permitir", params={"domain": "otro.ejemplo.com"}).status_code == 404
    assert cliente.get("/caddy/permitir").status_code == 404


def test_restablecer_contrasena_del_admin_se_muestra_una_vez(panel):
    cliente, llamadas = panel
    csrf = _entrar(cliente)
    cliente.post("/isp/nuevo", data={"csrf": csrf, "nombre": "A", "slug": "aaa"})
    respuesta = cliente.post("/isp/1/restablecer-admin", data={"csrf": csrf, "usuario": "admin"})
    _, slug, usuario, clave = [c for c in llamadas if c[0] == "restablecer"][0]
    assert (slug, usuario) == ("aaa", "admin") and len(clave) >= 16
    assert clave in respuesta.text and "Contraseña nueva" in respuesta.text
    detalle = cliente.get("/isp/1").text
    assert clave not in detalle and "contraseña restablecida" in detalle.lower()


# --- Google Drive del ISP ------------------------------------------------------

def _firmado(accion, isp="aaa", vence=None, secreto_panel="s" * 64):
    import time

    from nube.panel import drive

    vence = vence or int(time.time()) + 60
    firma = drive.firmar(drive.secreto_isp(secreto_panel, isp), isp, accion, vence)
    return {"isp": isp, "vence": str(vence), "firma": firma}


@pytest.fixture
def con_drive(panel, tmp_path, monkeypatch):
    from nube.panel import drive

    monkeypatch.setattr(drive, "CLIENTE_ID", "cliente.apps.googleusercontent.com")
    monkeypatch.setattr(drive, "CLIENTE_SECRETO", "secreto")
    monkeypatch.setattr(drive, "CLAVE_CIFRADO", "c" * 32)
    monkeypatch.setattr(drive, "CONFIGS", tmp_path / "drive")
    monkeypatch.setattr(drive, "listar", lambda slug: [])
    monkeypatch.setattr(drive, "canjear_codigo", lambda codigo, regreso: ({"refresh_token": "r"}, "red@gmail.com"))
    guardados = []

    def guardar(slug, token):
        (tmp_path / "drive").mkdir(exist_ok=True)
        (tmp_path / "drive" / f"{slug}.conf").write_text("token = {}")
        guardados.append((slug, token))

    monkeypatch.setattr(drive, "guardar_conexion", guardar)
    monkeypatch.setattr(drive, "desconectar", lambda slug: (tmp_path / "drive" / f"{slug}.conf").unlink())
    cliente, llamadas = panel
    csrf = _entrar(cliente)
    cliente.post("/isp/nuevo", data={"csrf": csrf, "nombre": "A", "slug": "aaa"})
    return cliente, llamadas, guardados


def test_alta_le_pasa_al_isp_su_enlace_con_el_panel(panel):
    cliente, llamadas = panel
    from nube.panel import drive

    csrf = _entrar(cliente)
    cliente.post("/isp/nuevo", data={"csrf": csrf, "nombre": "A", "slug": "aaa"})
    assert llamadas[0][3]["enlace"] == {
        "NUBE_PANEL_URL": "https://panel.ejemplo.com",
        "NUBE_PANEL_SECRETO": drive.secreto_isp("s" * 64, "aaa"),
    }


def test_drive_sin_firma_valida_se_rechaza(con_drive):
    cliente, _, _ = con_drive
    assert cliente.get("/drive/estado", params={**_firmado("estado"), "firma": "0" * 64}).status_code == 403
    # Firma de otra acción u otro ISP no sirve.
    assert cliente.get("/drive/estado", params=_firmado("desconectar")).status_code == 403
    assert cliente.get("/drive/estado", params=_firmado("estado", isp="otro")).status_code == 403
    vencida = _firmado("estado", vence=1_000_000_000)
    assert cliente.get("/drive/estado", params=vencida).status_code == 403


def test_conectar_drive_y_primer_respaldo(con_drive):
    cliente, llamadas, guardados = con_drive
    from urllib.parse import parse_qs, urlparse

    estado = cliente.get("/drive/estado", params=_firmado("estado")).json()
    assert estado["disponible"] and not estado["conectado"]

    google = cliente.get("/drive/conectar", params={**_firmado("conectar"), "correo": "red@gmail.com"})
    assert google.status_code == 303
    consulta = parse_qs(urlparse(google.headers["location"]).query)
    assert consulta["redirect_uri"] == ["https://panel.ejemplo.com/drive/regreso"]
    assert "https://www.googleapis.com/auth/drive.file" in consulta["scope"][0]
    assert consulta["login_hint"] == ["red@gmail.com"]

    regreso = cliente.get("/drive/regreso", params={"state": consulta["state"][0], "code": "codigo"})
    assert regreso.headers["location"] == "https://aaa.ejemplo.com/admin/configuracion/almacenamiento?drive=ok"
    assert guardados == [("aaa", {"refresh_token": "r"})]
    estado = cliente.get("/drive/estado", params=_firmado("estado")).json()
    assert estado["conectado"] and estado["correo"] == "red@gmail.com"
    assert "Drive conectado" in cliente.get("/isp/1").text


def test_regreso_sin_permiso_vuelve_al_isp_con_el_motivo(con_drive):
    cliente, _, guardados = con_drive
    import jwt

    estado = jwt.encode({"isp": "aaa", "exp": 9_999_999_999}, "s" * 64, algorithm="HS256")
    regreso = cliente.get("/drive/regreso", params={"state": estado, "error": "access_denied"})
    assert regreso.headers["location"].startswith("https://aaa.ejemplo.com/admin/configuracion/almacenamiento?drive=No+se")
    assert guardados == []
    assert cliente.get("/drive/regreso", params={"state": "falso", "code": "x"}).status_code == 400


def test_respaldo_con_drive_va_a_su_drive(con_drive, tmp_path, monkeypatch):
    cliente, _, _ = con_drive
    from nube.panel import docker_ops, drive

    destinos = []
    monkeypatch.setattr(docker_ops, "respaldar", lambda slug, destino=None: destinos.append(destino) or {
        "ARCHIVO": f"{slug}-20261029-030000.tar", "TAMANO": "10", "REMOTO": "ok"})
    csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', cliente.get("/").text).group(1)
    cliente.post("/isp/1/respaldar", data={"csrf": csrf})
    (tmp_path / "drive").mkdir(exist_ok=True)
    (tmp_path / "drive" / "aaa.conf").write_text("token = {}")
    cliente.post("/isp/1/respaldar", data={"csrf": csrf})
    assert destinos[0] is None
    assert destinos[1] == drive.ajustes_respaldo("aaa")
    assert destinos[1]["RESPALDO_REMOTO"] == "cifrado:"


def test_desconectar_drive(con_drive, tmp_path):
    cliente, _, _ = con_drive
    (tmp_path / "drive").mkdir(exist_ok=True)
    (tmp_path / "drive" / "aaa.conf").write_text("token = {}")
    from nube.panel import db

    with db.Sesion() as sesion:
        sesion.add(db.DriveIsp(isp_id=1, correo="red@gmail.com"))
        sesion.commit()
    assert cliente.get("/drive/estado", params=_firmado("estado")).json()["conectado"]
    estado = cliente.post("/drive/desconectar", params=_firmado("desconectar")).json()
    assert not estado["conectado"] and not (tmp_path / "drive" / "aaa.conf").exists()


def test_privacidad_y_terminos_son_publicos(panel):
    cliente, _ = panel
    privacidad = cliente.get("/privacidad")
    assert privacidad.status_code == 200 and "drive.file" in privacidad.text
    assert cliente.get("/terminos").status_code == 200
