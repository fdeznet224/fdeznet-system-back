import subprocess
from pathlib import Path


INSTALLER = Path(__file__).resolve().parents[1] / "install.sh"


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
