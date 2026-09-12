import asyncio
import hashlib
import hmac
from types import SimpleNamespace

from src.interfaces.api import configuracion, control_plane
from src.interfaces.api.control_plane import _sign_update_manifest


def test_firma_manifiesto_usa_clave_derivada_de_licencia():
    license_key = "fdz_live_prueba_segura"
    license_hash = hashlib.sha256(license_key.encode()).hexdigest()
    canonical = f"2.5.0|{'a' * 40}|{'b' * 40}"
    expected = hmac.new(
        bytes.fromhex(license_hash),
        canonical.encode(),
        hashlib.sha256,
    ).hexdigest()

    assert _sign_update_manifest(
        license_hash,
        "2.5.0",
        "a" * 40,
        "b" * 40,
    ) == expected


class _ReleaseResult:
    def __init__(self, release):
        self.release = release

    def scalar_one_or_none(self):
        return self.release


class _ReleaseDB:
    def __init__(self, release):
        self.release = release

    async def execute(self, _query):
        return _ReleaseResult(self.release)


def test_usuario_puede_solicitar_actualizacion_sin_activar_automaticas(monkeypatch):
    installation = SimpleNamespace(
        estado="activa",
        actualizacion_automatica=False,
        version_actual="2.5.2",
        version_objetivo="2.5.3",
        licencia_hash="a" * 64,
    )
    release = SimpleNamespace(
        version="2.5.3",
        backend_commit="b" * 40,
        frontend_commit="c" * 40,
        notas="Actualización solicitada por el usuario",
    )

    async def authenticate(*_args, **_kwargs):
        return installation

    monkeypatch.setattr(control_plane, "_ensure_central", lambda: None)
    monkeypatch.setattr(control_plane, "_authenticate_installation", authenticate)
    db = _ReleaseDB(release)

    async def run_checks():
        automatic = await control_plane.update_manifest(
            x_installation_id="installation-id",
            x_license_key="license-key",
            x_update_requested="",
            db=db,
        )
        manual = await control_plane.update_manifest(
            x_installation_id="installation-id",
            x_license_key="license-key",
            x_update_requested="true",
            db=db,
        )
        return automatic, manual

    automatic_check, manual_check = asyncio.run(run_checks())

    assert automatic_check.actualizacion_disponible is False
    assert manual_check.actualizacion_disponible is True
    assert manual_check.version == "2.5.3"


def test_endpoint_manual_deja_autorizacion_de_un_solo_uso(monkeypatch, tmp_path):
    request_file = tmp_path / "manual-update-requested"
    started = []

    async def start(service):
        started.append(service)
        return {"status": "ok", "mensaje": "La tarea inició en segundo plano"}

    monkeypatch.setattr(configuracion, "MANUAL_UPDATE_REQUEST_FILE", request_file)
    monkeypatch.setattr(configuracion, "_iniciar_mantenimiento", start)

    response = asyncio.run(configuracion.iniciar_actualizacion())

    assert response["status"] == "ok"
    assert request_file.read_text(encoding="utf-8") == "requested\n"
    assert started == ["fdeznet-update.service"]


def test_publicar_version_la_asigna_a_todas_sin_forzar_instalacion(monkeypatch):
    release = SimpleNamespace(id=7, version="2.15.0", notas="Nuevo panel", activa=True)

    class Result:
        rowcount = 4

    class DB:
        committed = False

        async def get(self, _model, release_id):
            assert release_id == 7
            return release

        async def execute(self, _statement):
            return Result()

        async def commit(self):
            self.committed = True

    monkeypatch.setattr(control_plane, "_ensure_central", lambda: None)
    db = DB()
    response = asyncio.run(control_plane.publish_release_to_all(7, db=db))

    assert response.version == "2.15.0"
    assert response.instalaciones_asignadas == 4
    assert "cada cliente decide" in response.mensaje
    assert db.committed is True


def test_eliminar_instalacion_es_definitivo(monkeypatch):
    installation = SimpleNamespace(id=12)

    class DB:
        deleted = None
        committed = False

        async def get(self, _model, installation_id):
            assert installation_id == 12
            return installation

        async def delete(self, value):
            self.deleted = value

        async def commit(self):
            self.committed = True

    monkeypatch.setattr(control_plane, "_ensure_central", lambda: None)
    db = DB()
    response = asyncio.run(control_plane.delete_installation(12, db=db))

    assert response.status_code == 204
    assert db.deleted is installation
    assert db.committed is True
