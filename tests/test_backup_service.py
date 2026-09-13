import json
import os
import asyncio
from datetime import datetime

import pytest

from src.application.services import backup_service
from src.domain.schemas import BackupPolicyUpdate
from src.interfaces.api import configuracion


@pytest.fixture
def backup_paths(tmp_path, monkeypatch):
    backup_dir = tmp_path / "backups"
    state_dir = tmp_path / "state"
    backup_dir.mkdir()
    monkeypatch.setattr(backup_service, "BACKUP_DIR", backup_dir)
    monkeypatch.setattr(backup_service, "POLICY_FILE", state_dir / "policy.json")
    monkeypatch.setattr(backup_service, "OPERATION_FILE", state_dir / "operation.json")
    monkeypatch.setattr(backup_service, "STATUS_FILE", state_dir / "status.json")
    return backup_dir, state_dir


def test_guarda_y_recupera_politica_de_respaldos(backup_paths):
    _, state_dir = backup_paths
    policy = BackupPolicyUpdate(
        activo=True,
        frecuencia_dias=7,
        retencion_dias=90,
        incluir_configuracion=False,
        incluir_archivos_estaticos=True,
        incluir_evidencias_ordenes=False,
        incluir_sesion_whatsapp=True,
        incluir_archivos_whatsapp=False,
        incluir_wireguard=True,
    )

    saved = backup_service.save_backup_policy(policy)

    assert backup_service.load_backup_policy() == saved
    assert json.loads((state_dir / "policy.json").read_text()) == saved
    assert (state_dir / "policy.json").stat().st_mode & 0o777 == 0o600


def test_lista_respaldos_validos_con_checksum(backup_paths):
    backup_dir, _ = backup_paths
    older = backup_dir / "20260910-030000.tar.gz.gpg"
    newest = backup_dir / "20260912-030000.tar.gz.gpg"
    older.write_bytes(b"old")
    newest.write_bytes(b"new-backup")
    newest.with_suffix(newest.suffix + ".sha256").write_text("checksum")
    (backup_dir / "otro-archivo.tar.gz.gpg").write_bytes(b"ignored")
    os.utime(older, (1_757_473_200, 1_757_473_200))
    os.utime(newest, (1_757_646_000, 1_757_646_000))

    backups = backup_service.list_backups()

    assert [item["archivo"] for item in backups] == [newest.name, older.name]
    assert backups[0]["checksum_disponible"] is True
    assert backups[0]["bytes"] == len(b"new-backup")


def test_solicitud_es_exclusiva_y_rechaza_rutas_o_enlaces(backup_paths):
    backup_dir, state_dir = backup_paths
    filename = "20260912-030000.tar.gz.gpg"
    (backup_dir / filename).write_bytes(b"backup")

    backup_service.request_backup_operation("verificar", filename)

    assert json.loads((state_dir / "operation.json").read_text()) == {
        "accion": "verificar",
        "archivo": filename,
    }
    with pytest.raises(ValueError, match="en curso"):
        backup_service.request_backup_operation("restaurar", filename)
    with pytest.raises(ValueError, match="inválido"):
        backup_service.backup_path("../secreto")

    (state_dir / "operation.json").unlink()
    link = backup_dir / "20260911-030000.tar.gz.gpg"
    link.symlink_to(backup_dir / filename)
    with pytest.raises(FileNotFoundError):
        backup_service.backup_path(link.name)


def test_panel_calcula_siguiente_respaldo_y_lee_estado(backup_paths):
    backup_dir, state_dir = backup_paths
    filename = backup_dir / "20260912-030000.tar.gz.gpg"
    filename.write_bytes(b"backup")
    timestamp = datetime(2026, 9, 12, 3, 0).timestamp()
    os.utime(filename, (timestamp, timestamp))
    backup_service.save_backup_policy(
        BackupPolicyUpdate(frecuencia_dias=3, retencion_dias=30)
    )
    state_dir.mkdir(exist_ok=True)
    (state_dir / "status.json").write_text(
        json.dumps({"estado": "respaldado", "mensaje": "Correcto"})
    )

    dashboard = backup_service.backup_dashboard()

    assert dashboard["estado"]["estado"] == "respaldado"
    assert datetime.fromisoformat(dashboard["proximo_respaldo"]).date().isoformat() == "2026-09-15"


def test_tarea_privilegiada_se_solicita_sin_sudo(tmp_path, monkeypatch):
    request_file = tmp_path / "backup-requested"
    monkeypatch.setitem(
        configuracion.MAINTENANCE_REQUESTS,
        "fdeznet-backup.service",
        request_file,
    )

    response = asyncio.run(
        configuracion._iniciar_mantenimiento("fdeznet-backup.service")
    )

    assert response["status"] == "ok"
    assert request_file.read_text() == "requested\n"
    assert request_file.stat().st_mode & 0o777 == 0o600
