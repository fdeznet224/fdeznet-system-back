import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path


BACKUP_DIR = Path(os.getenv("FDEZNET_BACKUP_DIR", "/var/backups/fdeznet"))
POLICY_FILE = Path("/var/lib/fdeznet/backup-policy.json")
OPERATION_FILE = Path("/var/lib/fdeznet/backup-operation.json")
STATUS_FILE = Path("/var/lib/fdeznet/maintenance-status.json")
BACKUP_NAME_PATTERN = re.compile(r"^\d{8}-\d{6}\.tar\.gz\.gpg$")


def default_backup_policy() -> dict:
    try:
        retention = int(os.getenv("FDEZNET_BACKUP_RETENTION_DAYS", "14"))
    except ValueError:
        retention = 14
    return {
        "activo": True,
        "frecuencia_dias": 1,
        "retencion_dias": min(365, max(3, retention)),
        "incluir_configuracion": True,
        "incluir_archivos_estaticos": True,
        "incluir_evidencias_ordenes": True,
        "incluir_sesion_whatsapp": True,
        "incluir_archivos_whatsapp": True,
        "incluir_wireguard": True,
    }


def load_backup_policy() -> dict:
    policy = default_backup_policy()
    try:
        saved = json.loads(POLICY_FILE.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            policy.update({key: saved[key] for key in policy if key in saved})
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        pass
    return policy


def save_backup_policy(data) -> dict:
    policy = data.model_dump()
    POLICY_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = POLICY_FILE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(policy, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(POLICY_FILE)
    return policy


def backup_path(filename: str) -> Path:
    if not BACKUP_NAME_PATTERN.fullmatch(filename):
        raise ValueError("Nombre de respaldo inválido")
    raw_candidate = BACKUP_DIR / filename
    if raw_candidate.is_symlink():
        raise FileNotFoundError("Respaldo no encontrado")
    candidate = raw_candidate.resolve()
    if candidate.parent != BACKUP_DIR.resolve() or not candidate.is_file():
        raise FileNotFoundError("Respaldo no encontrado")
    return candidate


def list_backups() -> list[dict]:
    backups = []
    try:
        candidates = BACKUP_DIR.glob("*.tar.gz.gpg")
        for path in candidates:
            if not BACKUP_NAME_PATTERN.fullmatch(path.name) or path.is_symlink():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            backups.append(
                {
                    "archivo": path.name,
                    "creado_en": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    "bytes": stat.st_size,
                    "checksum_disponible": path.with_suffix(
                        path.suffix + ".sha256"
                    ).is_file(),
                }
            )
    except OSError:
        return []
    return sorted(backups, key=lambda item: item["creado_en"], reverse=True)


def backup_dashboard() -> dict:
    policy = load_backup_policy()
    backups = list_backups()
    status = None
    try:
        status = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        pass
    next_backup = None
    if policy["activo"] and backups:
        next_backup = (
            datetime.fromisoformat(backups[0]["creado_en"])
            + timedelta(days=policy["frecuencia_dias"])
        ).isoformat()
    return {
        "politica": policy,
        "respaldos": backups,
        "proximo_respaldo": next_backup,
        "estado": status,
    }


def request_backup_operation(action: str, filename: str) -> Path:
    if action not in {"verificar", "restaurar"}:
        raise ValueError("Operación de respaldo inválida")
    path = backup_path(filename)
    OPERATION_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            OPERATION_FILE,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError as exc:
        raise ValueError("Ya hay una verificación o restauración en curso") from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as operation:
        json.dump({"accion": action, "archivo": path.name}, operation)
        operation.write("\n")
    return path
