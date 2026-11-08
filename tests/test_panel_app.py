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
    monkeypatch.setattr(docker_ops, "borrar", lambda slug: llamadas.append(("borrar", slug)))
    from nube.panel import whatsapp
    monkeypatch.setattr(whatsapp, "estado", lambda: {"disponible": True, "conectado": True, "iniciado": True, "qr": None})
    monkeypatch.setattr(whatsapp, "enviar", lambda numero, mensaje: llamadas.append(("whatsapp", numero, mensaje)))
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
    cliente.post("/isp/nuevo", data={"csrf": csrf, "nombre": "Colega", "slug": "colega"})
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


def _isp_vencido(cliente, csrf, dias_vencido: int):
    from datetime import timedelta

    from nube.panel.db import Isp, Sesion

    cliente.post("/isp/nuevo", data={"csrf": csrf, "nombre": "Moroso", "slug": "moroso", "precio_mensual": "800"})
    with Sesion() as db:
        db.get(Isp, 1).pagado_hasta = date.today() - timedelta(days=dias_vencido)
        db.commit()


def test_corte_automatico_suspende_al_terminar_la_gracia(panel):
    from nube.panel import app as app_mod
    from nube.panel.db import Bitacora, Isp, Sesion

    cliente, llamadas = panel
    csrf = _entrar(cliente)
    _isp_vencido(cliente, csrf, 5)  # último día de gracia
    assert app_mod._revisar_cortes(date.today()) == []
    assert "se suspende hoy" not in cliente.get("/").text

    with Sesion() as db:
        db.get(Isp, 1).pagado_hasta = date(2026, 1, 1)
        db.commit()
    assert app_mod._revisar_cortes(date.today()) == ["moroso"]
    assert ("detener", "moroso") in llamadas
    with Sesion() as db:
        isp = db.get(Isp, 1)
        assert isp.suspendido and isp.suspension_automatica
        assert db.query(Bitacora).filter_by(accion="suspendido automático").count() == 1
    assert app_mod._revisar_cortes(date.today()) == []  # no lo repite
    inicio = cliente.get("/").text
    assert "Suspendido por falta de pago" in inicio


def test_pago_reactiva_lo_suspendido_por_el_corte(panel):
    from nube.panel import app as app_mod
    from nube.panel.db import Isp, Sesion

    cliente, llamadas = panel
    csrf = _entrar(cliente)
    _isp_vencido(cliente, csrf, 30)
    app_mod._revisar_cortes(date.today())
    respuesta = cliente.post("/isp/1/pago", data={
        "csrf": csrf, "monto": "800", "meses": "1", "fecha": date.today().isoformat()})
    assert "reactivado" in respuesta.headers["location"]
    assert ("encender", "moroso") in llamadas
    with Sesion() as db:
        assert not db.get(Isp, 1).suspendido


def test_pago_no_reactiva_lo_suspendido_a_mano(panel):
    cliente, llamadas = panel
    csrf = _entrar(cliente)
    _isp_vencido(cliente, csrf, 30)
    cliente.post("/isp/1/suspender", data={"csrf": csrf})
    cliente.post("/isp/1/pago", data={"csrf": csrf, "monto": "800", "meses": "1", "fecha": date.today().isoformat()})
    assert ("encender", "moroso") not in llamadas


def test_reactivar_a_un_moroso_le_da_prorroga(panel):
    from datetime import timedelta

    from nube.panel import app as app_mod
    from nube.panel.db import Isp, Sesion

    cliente, _ = panel
    csrf = _entrar(cliente)
    _isp_vencido(cliente, csrf, 30)
    app_mod._revisar_cortes(date.today())
    assert "Prórroga hasta" in cliente.get("/isp/1").text
    hasta = date.today() + timedelta(days=2)
    cliente.post("/isp/1/reactivar", data={"csrf": csrf, "prorroga": hasta.isoformat()})
    with Sesion() as db:
        isp = db.get(Isp, 1)
        assert not isp.suspendido and isp.prorroga_hasta == hasta
    assert app_mod._revisar_cortes(date.today()) == []
    assert app_mod._revisar_cortes(hasta + timedelta(days=1)) == ["moroso"]


def test_sin_corte_automatico_no_se_suspende(panel):
    from nube.panel import app as app_mod

    cliente, _ = panel
    csrf = _entrar(cliente)
    _isp_vencido(cliente, csrf, 30)
    cliente.post("/isp/1/datos", data={"csrf": csrf, "nombre": "Moroso", "precio_mensual": "800", "dia_pago": "1",
                                       "pagado_hasta": "2026-01-01"})
    assert app_mod._revisar_cortes(date.today()) == []
    assert "sin corte automático" in cliente.get("/").text


def test_inicio_filtra_y_muestra_lo_pendiente(panel):
    cliente, _ = panel
    csrf = _entrar(cliente)
    _isp_vencido(cliente, csrf, 2)
    inicio = cliente.get("/").text
    assert "Requiere atención" in inicio and "en gracia" in inicio
    assert "Moroso" in cliente.get("/?ver=cobrar").text
    assert "Ninguno en esta lista" in cliente.get("/?ver=suspendidos").text


def test_el_isp_consulta_su_aviso_de_pago_firmado(panel):
    import time
    from datetime import timedelta

    from nube.panel import drive
    from nube.panel.db import Isp, Sesion

    cliente, _ = panel
    csrf = _entrar(cliente)
    cliente.post("/isp/nuevo", data={"csrf": csrf, "nombre": "Colega", "slug": "colega"})
    vence = int(time.time()) + 60
    firma = drive.firmar(drive.secreto_isp("s" * 64, "colega"), "colega", "cuenta", vence)
    pedir = lambda f: cliente.get("/cuenta/estado", params={"isp": "colega", "vence": vence, "firma": f})
    assert pedir("mala").status_code == 403
    assert pedir(firma).json()["aviso"] is None  # sin pagos registrados

    with Sesion() as db:
        db.get(Isp, 1).pagado_hasta = date.today() - timedelta(days=1)
        db.commit()
    aviso = pedir(firma).json()["aviso"]
    assert aviso["tono"] == "rojo" and "se suspende" in aviso["mensaje"]


# --- Demos desde la página de registro ---------------------------------------

class _HiloInmediato:
    def __init__(self, target, args=(), **_):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


@pytest.fixture
def registro(panel, monkeypatch):
    from nube.panel import app as app_mod

    monkeypatch.setattr(app_mod.threading, "Thread", _HiloInmediato)
    monkeypatch.setattr(app_mod, "_ram_libre_gb", lambda: 6.0)
    return panel


DATOS = {"empresa": "Red Rápida S.A.", "nombre": "Ana López", "telefono": "961 363 2496", "correo": "ana@red.mx",
         "clientes": "150"}


def test_registro_crea_el_demo_y_manda_su_acceso(registro):
    from nube.panel.db import Isp, Sesion

    cliente, llamadas = registro
    respuesta = cliente.post("/api/registro", json=DATOS).json()
    assert respuesta["ok"] and respuesta["direccion"] == "https://red-rapida.ejemplo.com"
    _, slug, _, opciones = next(c for c in llamadas if c[0] == "crear")
    assert slug == "red-rapida" and opciones["whatsapp"] is False and opciones["limite_clientes"] == 25
    assert opciones["marca"] == {"MARCA_NOMBRE": "Red Rápida S.A.", "MARCA_TELEFONO": "9613632496",
                                 "MARCA_CORREO": "ana@red.mx"}
    _, numero, mensaje = next(c for c in llamadas if c[0] == "whatsapp")
    assert numero == "529613632496"
    assert "ClaveInicial123" in mensaje and "https://red-rapida.ejemplo.com" in mensaje and "Ana" in mensaje
    with Sesion() as db:
        isp = db.query(Isp).one()
        assert isp.es_demo and isp.demo_pasos is None and isp.precio_mensual == 0
        assert isp.demo_hasta == date.today() + __import__("datetime").timedelta(days=15)

    otra_vez = cliente.post("/api/registro", json=DATOS).json()
    assert otra_vez.get("existente") and sum(1 for c in llamadas if c[0] == "crear") == 1


def test_registro_valida_y_frena_robots(registro):
    cliente, llamadas = registro
    assert cliente.post("/api/registro", json={**DATOS, "telefono": "12345"}).status_code == 400
    assert cliente.post("/api/registro", json={**DATOS, "empresa": ""}).status_code == 400
    assert cliente.post("/api/registro", json={**DATOS, "sitio_web": "spam"}).json()["ok"]
    assert not any(c[0] == "crear" for c in llamadas)
    for numero in ("9610000001", "9610000002", "9610000003"):
        assert cliente.post("/api/registro", json={**DATOS, "telefono": numero}).status_code == 200
    assert cliente.post("/api/registro", json={**DATOS, "telefono": "9610000004"}).status_code == 429


def test_sin_whatsapp_el_registro_queda_en_espera(registro, monkeypatch):
    from nube.panel import whatsapp
    from nube.panel.db import Bitacora, Isp, Sesion

    cliente, llamadas = registro
    monkeypatch.setattr(whatsapp, "estado", lambda: {"disponible": True, "conectado": False, "iniciado": True, "qr": "x"})
    assert cliente.post("/api/registro", json=DATOS).json()["espera"]
    with Sesion() as db:
        assert db.query(Isp).count() == 0
        assert "Ana López" in db.query(Bitacora).filter_by(accion="registro en espera").one().detalle


def test_ciclo_del_demo_avisa_pausa_y_borra(registro):
    from datetime import timedelta

    from nube.panel import app as app_mod
    from nube.panel.db import Bitacora, Isp, Sesion

    cliente, llamadas = registro
    cliente.post("/api/registro", json=DATOS)
    with Sesion() as db:
        hasta = db.query(Isp).one().demo_hasta
    llamadas.clear()
    assert app_mod._revisar_demos(hasta - timedelta(days=10)) == []
    assert app_mod._revisar_demos(hasta - timedelta(days=5)) == [("red-rapida", "aviso")]
    assert app_mod._revisar_demos(hasta - timedelta(days=5)) == []  # no repite
    assert app_mod._revisar_demos(hasta + timedelta(days=1)) == [("red-rapida", "fin")]
    assert ("detener", "red-rapida") in llamadas
    assert app_mod._revisar_demos(hasta + timedelta(days=13)) == [("red-rapida", "aviso_borrar")]
    assert app_mod._revisar_demos(hasta + timedelta(days=16)) == [("red-rapida", "borrar")]
    assert ("borrar", "red-rapida") in llamadas
    mensajes = [c[2] for c in llamadas if c[0] == "whatsapp"]
    assert len(mensajes) == 3
    assert "termina el" in mensajes[0] and "quedó en pausa" in mensajes[1] and "se borran para siempre" in mensajes[2]
    with Sesion() as db:
        assert db.query(Isp).count() == 0
        assert db.query(Bitacora).filter_by(accion="demo borrado").count() == 1


def test_pago_convierte_el_demo_en_cliente_y_lo_reactiva(registro):
    from datetime import timedelta

    from nube.panel import app as app_mod
    from nube.panel.db import Isp, Sesion

    cliente, llamadas = registro
    csrf = _entrar(cliente)
    cliente.post("/api/registro", json=DATOS)
    with Sesion() as db:
        hasta = db.query(Isp).one().demo_hasta
    app_mod._revisar_demos(hasta + timedelta(days=1))  # se pausa
    cliente.post("/isp/1/pago", data={"csrf": csrf, "monto": "500", "meses": "1", "fecha": date.today().isoformat()})
    assert ("encender", "red-rapida") in llamadas
    with Sesion() as db:
        isp = db.get(Isp, 1)
        assert not isp.es_demo and not isp.suspendido
    assert app_mod._revisar_demos(hasta + timedelta(days=20)) == []


def test_alargar_demo_y_reenviar_acceso(registro):
    from datetime import timedelta

    from nube.panel.db import Isp, Sesion

    cliente, llamadas = registro
    csrf = _entrar(cliente)
    cliente.post("/api/registro", json=DATOS)
    nueva = date.today() + timedelta(days=45)
    cliente.post("/isp/1/demo", data={"csrf": csrf, "demo_hasta": nueva.isoformat()})
    with Sesion() as db:
        assert db.get(Isp, 1).demo_hasta == nueva
    cliente.post("/isp/1/demo/reenviar", data={"csrf": csrf})
    assert any(c[0] == "restablecer" for c in llamadas)
    assert sum(1 for c in llamadas if c[0] == "whatsapp") == 2
    pagina = cliente.get("/?ver=demos").text
    assert "Red Rápida" in pagina and "Demo" in pagina
    assert "Ninguno en esta lista" in cliente.get("/?ver=cobrar").text


def test_webhook_del_bot_pide_su_secreto(panel, monkeypatch):
    from nube.panel import whatsapp

    cliente, _ = panel
    monkeypatch.setattr(whatsapp, "SECRETO", "secreto-del-bot")
    assert cliente.post("/whatsapp/webhook/recibir", json={}).status_code == 403
    assert cliente.post("/whatsapp/webhook/recibir", json={}, headers={"X-Webhook-Secret": "secreto-del-bot"}).json()["ok"]


def test_subdominio_reservado_no_se_da_de_alta(panel):
    cliente, llamadas = panel
    csrf = _entrar(cliente)
    assert cliente.post("/isp/nuevo", data={"csrf": csrf, "nombre": "X", "slug": "panel"}).status_code == 400
    assert llamadas == []


def test_interesado_en_fichas_queda_para_contactarlo(registro):
    cliente, llamadas = registro
    assert cliente.post("/api/interes", json={"empresa": "Ciber Luna", "nombre": "Juan", "telefono": "12"}).status_code == 400
    respuesta = cliente.post("/api/interes", json={"empresa": "Ciber Luna", "nombre": "Juan Pérez", "telefono": "9611112233"})
    assert respuesta.json()["ok"]
    assert not any(c[0] == "crear" for c in llamadas)
    _, numero, mensaje = next(c for c in llamadas if c[0] == "whatsapp")
    assert numero == "529611112233" and "WisPay Fichas" in mensaje
    _entrar(cliente)
    inicio = cliente.get("/").text
    assert "Por contactar" in inicio and "Ciber Luna" in inicio and "wa.me/529611112233" in inicio


# --- Versiones liberadas y actualización desde el ISP -------------------------

def test_liberar_version_y_el_isp_actualiza(panel, monkeypatch):
    import time
    from nube.panel import app as app_mod, docker_ops, drive
    from nube.panel.db import Isp, Sesion

    cliente, llamadas = panel
    monkeypatch.setattr(app_mod.threading, "Thread", _HiloInmediato)
    monkeypatch.setattr(docker_ops, "versiones_disponibles", lambda: ["2.115.0", "2.110.1"])
    csrf = _entrar(cliente)
    cliente.post("/isp/nuevo", data={"csrf": csrf, "nombre": "Colega", "slug": "colega", "version": "2.110.1"})
    vence = int(time.time()) + 60
    firma = lambda accion: drive.firmar(drive.secreto_isp("s" * 64, "colega"), "colega", accion, vence)
    estado = lambda: cliente.get("/cuenta/estado", params={"isp": "colega", "vence": vence, "firma": firma("cuenta")}).json()
    actualizar = lambda f: cliente.post("/cuenta/actualizar", params={"isp": "colega", "vence": vence, "firma": f})

    assert estado()["version"] == {"instalada": "2.110.1", "disponible": None, "novedades": None,
                                   "fecha": None, "actualizando": False}
    assert actualizar(firma("actualizar")).status_code == 409  # nada liberado
    assert actualizar(firma("cuenta")).status_code == 403  # la firma es por acción

    assert "construida" in cliente.post("/versiones/liberar", data={"csrf": csrf, "version": "9.9.9"}).headers["location"]
    cliente.post("/versiones/liberar", data={"csrf": csrf, "version": "2.115.0", "novedades": "Entrar con huella"})
    pagina = cliente.get("/versiones").text
    assert "v2.115.0" in pagina and "sin actualizar" in pagina
    version = estado()["version"]
    assert version["disponible"] == "2.115.0" and version["novedades"] == "Entrar con huella"

    llamadas.clear()
    assert actualizar(firma("actualizar")).json() == {"ok": True, "version": "2.115.0"}
    assert llamadas == [("ajustes", "colega", {"version": "2.115.0", "whatsapp": False, "limite_clientes": None,
                                                "limite_routers": None, "encendido": True})]
    with Sesion() as db:
        assert db.get(Isp, 1).version == "2.115.0"
    assert estado()["version"]["disponible"] is None
    assert actualizar(firma("actualizar")).status_code == 409


def test_cuenta_estado_dias_restantes(panel):
    import time
    from datetime import timedelta
    from nube.panel import drive
    from nube.panel.db import Isp, Sesion

    cliente, _ = panel
    csrf = _entrar(cliente)
    cliente.post("/isp/nuevo", data={"csrf": csrf, "nombre": "Colega", "slug": "colega", "plan": "800"})
    with Sesion() as db:
        db.get(Isp, 1).pagado_hasta = date.today() + timedelta(days=12)
        db.commit()
    vence = int(time.time()) + 60
    firma = drive.firmar(drive.secreto_isp("s" * 64, "colega"), "colega", "cuenta", vence)
    cuenta = cliente.get("/cuenta/estado", params={"isp": "colega", "vence": vence, "firma": firma}).json()["cuenta"]
    assert cuenta["dias_restantes"] == 12 and cuenta["es_demo"] is False
    assert cuenta["vigente_hasta"] == (date.today() + timedelta(days=12)).isoformat()


# --- ACS (TR-069) que activa el ISP -------------------------------------------

def test_el_isp_activa_su_acs(panel, monkeypatch):
    import time
    from nube.panel import app as app_mod, docker_ops, drive
    from nube.panel.db import Bitacora, Isp, Sesion
    from sqlalchemy import select

    cliente, llamadas = panel
    monkeypatch.setattr(app_mod.threading, "Thread", _HiloInmediato)
    lista = {"imagen": False}
    monkeypatch.setattr(docker_ops, "imagen_acs_lista", lambda: lista["imagen"])
    monkeypatch.setattr(docker_ops, "activar_acs", lambda slug, dominio, encendido: llamadas.append(
        ("acs", slug, dominio, encendido)) or {"ACS_URL": f"http://{dominio}:7548", "ACS_USUARIO": "u", "ACS_CLAVE": "c"})
    csrf = _entrar(cliente)
    cliente.post("/isp/nuevo", data={"csrf": csrf, "nombre": "Colega", "slug": "colega"})
    vence = int(time.time()) + 60
    firma = lambda accion: drive.firmar(drive.secreto_isp("s" * 64, "colega"), "colega", accion, vence)
    activar = lambda f: cliente.post("/cuenta/acs", params={"isp": "colega", "vence": vence, "firma": f})

    assert activar(firma("cuenta")).status_code == 403  # la firma es por acción
    assert activar(firma("acs")).status_code == 409  # la imagen no está construida
    lista["imagen"] = True
    llamadas.clear()
    assert activar(firma("acs")).json() == {"ok": True}
    assert llamadas == [("acs", "colega", "colega.ejemplo.com", True)]
    with Sesion() as db:
        acciones = db.scalars(select(Bitacora.accion).where(Bitacora.isp_id == 1)).all()
        assert "ACS activado" in acciones
        db.get(Isp, 1).suspendido = True
        db.commit()
    assert activar(firma("acs")).status_code == 409  # suspendido


def test_puerto_y_perfil_del_acs(tmp_path, monkeypatch):
    from nube.panel import docker_ops

    monkeypatch.setattr(docker_ops, "NUBE_DIR", tmp_path)
    for slug, extra in (("uno", "ACS_PORT=7548\n"), ("dos", "")):
        inst = tmp_path / "instancias" / slug
        inst.mkdir(parents=True)
        (inst / "compose.env").write_text(f"ISP={slug}\nIMAGEN_VERSION=1\nCOMPOSE_PROFILES=whatsapp\n{extra}")
        (inst / "api.env").write_text("FDEZNET_LIMITE_CLIENTES=\n")
    assert docker_ops.puerto_acs("uno") == 7548  # conserva el suyo
    assert docker_ops.puerto_acs("dos") == 7549  # el siguiente libre

    compose = []
    monkeypatch.setattr(docker_ops, "_compose", lambda slug, *a, **k: compose.append((slug, a)))
    monkeypatch.setattr(docker_ops, "_correr", lambda *a, **k: "")
    (tmp_path / "instancias" / "uno" / "acs.env").write_text("ACS_USUARIO=x\n")
    for slug in ("uno", "dos"):
        docker_ops.aplicar_ajustes(slug, version="2.117.0", whatsapp=False, limite_clientes=None,
                                   limite_routers=None, encendido=True)
    # Quitar el bot no apaga el ACS.
    assert "COMPOSE_PROFILES=acs\n" in (tmp_path / "instancias" / "uno" / "compose.env").read_text()
    assert "COMPOSE_PROFILES=\n" in (tmp_path / "instancias" / "dos" / "compose.env").read_text()
