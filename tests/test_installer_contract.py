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


def test_instalador_valida_recursos_y_compatibilidad_de_mysql():
    content = INSTALLER.read_text(encoding="utf-8")
    assert "MIN_MEMORY_KB" in content
    assert "MIN_DISK_BYTES" in content
    assert "x86_64/amd64" in content
    assert "default-mysql-server" in content


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
    assert "location /media/uploads/" in content
    assert "PUBLIC_URL=https://${DOMAIN}/media" in content


def test_instalador_recibe_identidad_inicial_del_panel_central():
    content = INSTALLER.read_text(encoding="utf-8")
    assert "FDEZNET_BRAND_NAME" in content
    assert "FDEZNET_BRAND_SYSTEM_NAME" in content
    assert "FDEZNET_BRAND_EMAIL" in content
    assert ".nombre_isp // empty" in content
    assert ".contacto_email // empty" in content


def test_instalador_fija_los_commits_autorizados_por_el_panel():
    content = INSTALLER.read_text(encoding="utf-8")
    assert "FDEZNET_RELEASE_VERSION" in content
    assert "FDEZNET_BACKEND_COMMIT" in content
    assert "FDEZNET_FRONTEND_COMMIT" in content
    assert 'reset --hard "$BACKEND_COMMIT"' in content
    assert 'reset --hard "$FRONTEND_COMMIT"' in content


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
    assert 'END {exit !found}' in content
    assert "make_url" in content
    assert "DATABASE_URL_VALUE" in content
    assert "X-Update-Requested: true" in content
    assert "manual-update-requested" in content
    assert "install_deployment_files" in content
    assert "systemctl daemon-reload" in content
    assert "location /media/uploads/" in content
    assert "nginx -t" in content
    assert 'config/nginx.conf" ]] && install' in content
    assert 'config/wireguard/." /etc/wireguard/' in content
    assert "exclude)bot_whatsapp/node_modules" in content
    assert "exclude)node_modules" in content
