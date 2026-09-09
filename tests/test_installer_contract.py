import subprocess
from pathlib import Path


INSTALLER = Path(__file__).resolve().parents[1] / "install.sh"
MAINTENANCE = Path(__file__).resolve().parents[1] / "scripts/fdeznet-maintenance.sh"


def test_instalador_tiene_sintaxis_bash_valida():
    result = subprocess.run(
        ["bash", "-n", str(INSTALLER)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_instalador_no_elimina_directorio_de_aplicacion():
    content = INSTALLER.read_text(encoding="utf-8")
    assert "rm -rf" not in content
    assert "merge --ff-only" in content
    assert "certbot --nginx" in content
    assert "FDEZNET_INSTALLATION_ID" in content


def test_instalador_configura_respaldo_y_revision_automatica():
    content = INSTALLER.read_text(encoding="utf-8")
    assert "gnupg" in content
    assert "backup.key" in content
    assert "fdeznet-backup.timer" in content
    assert "fdeznet-update.timer" in content
    assert "fdeznet-verify.timer" in content
    assert "FDEZNET_BACKUP_RETENTION_DAYS" in content
    assert "rclone" not in content.lower()


def test_agente_de_mantenimiento_tiene_sintaxis_y_reversion():
    result = subprocess.run(
        ["bash", "-n", str(MAINTENANCE)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    content = MAINTENANCE.read_text(encoding="utf-8")
    assert "rclone" not in content.lower()
    assert "gpg --batch" in content
    assert "sha256sum -c" in content
    assert "restore_backup \"$LAST_BACKUP\"" in content
    assert 'merge-base --is-ancestor "$OLD_BACKEND" "$backend_commit"' in content
    assert 'wait_for_health' in content
    assert 'verify_backup' in content
    assert "make_url" in content
    assert "DATABASE_URL_VALUE" in content
    assert "exclude)bot_whatsapp/node_modules" in content
    assert "exclude)node_modules" in content
