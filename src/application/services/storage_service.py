import asyncio
import os
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.models import (
    CierreAlmacenamientoModel,
    ComprobantePagoRevisionModel,
    ConfiguracionSistema,
    MensajeChatModel,
)


APP_ROOT = Path(__file__).resolve().parents[3]
RECEIPT_UPLOADS = APP_ROOT / "bot_whatsapp" / "uploads"
INVOICE_PDFS = APP_ROOT / "static" / "recibos"
ORDER_EVIDENCE = APP_ROOT / "uploads" / "ordenes"
BRANDING_FILES = APP_ROOT / "static" / "branding"
WHATSAPP_SESSION = APP_ROOT / "bot_whatsapp" / ".wwebjs_auth"
BACKUP_DIR = Path(os.getenv("FDEZNET_BACKUP_DIR", "/var/backups/fdeznet"))
PERIOD_PATTERN = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])$")


def directory_stats(path: Path) -> dict:
    files = 0
    size = 0
    accessible = True
    try:
        if path.is_dir():
            for item in path.rglob("*"):
                try:
                    if item.is_file() and not item.is_symlink():
                        files += 1
                        size += item.stat().st_size
                except OSError:
                    accessible = False
    except OSError:
        accessible = False
    return {"archivos": files, "bytes": size, "accesible": accessible}


def _safe_file_from_url(value: str | None, root: Path) -> Path | None:
    if not value:
        return None
    name = Path(urlparse(value).path).name
    if not name:
        return None
    candidate = (root / name).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def _period_bounds(period: str) -> tuple[datetime, datetime]:
    if not PERIOD_PATTERN.fullmatch(period):
        raise ValueError("El periodo debe tener formato AAAA-MM")
    year, month = (int(value) for value in period.split("-"))
    start = datetime(year, month, 1)
    if month == 12:
        end = datetime(year + 1, 1, 1)
    else:
        end = datetime(year, month + 1, 1)
    return start, end


def previous_period(now: datetime | None = None) -> str:
    current = now or datetime.now()
    first = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (first - timedelta(days=1)).strftime("%Y-%m")


def policy_from_config(config: ConfiguracionSistema) -> dict:
    return {
        "limpieza_automatica": bool(config.limpieza_almacenamiento_automatica),
        "hora_limpieza": config.hora_limpieza_almacenamiento or "02:30",
        "comprobantes_rechazados_dias": config.dias_retencion_comprobantes_rechazados or 90,
        "comprobantes_aprobados_dias": config.dias_retencion_comprobantes_aprobados or 365,
        "recibos_pdf_dias": config.dias_retencion_recibos_pdf or 180,
        "archivos_whatsapp_dias": config.dias_retencion_archivos_whatsapp or 90,
        "respaldos_dias": config.dias_retencion_respaldos or 14,
        "cierre_mensual_automatico": bool(config.cierre_mensual_automatico),
        "dia_cierre": config.dia_cierre_almacenamiento or 1,
        "ultima_limpieza": config.ultima_limpieza_almacenamiento,
        "ultimo_cierre": config.ultimo_cierre_almacenamiento,
    }


async def storage_dashboard(db: AsyncSession, config: ConfiguracionSistema) -> dict:
    disk = shutil.disk_usage(APP_ROOT)
    categories = []
    definitions = (
        ("comprobantes", "Comprobantes de WhatsApp", RECEIPT_UPLOADS, False),
        ("facturas_pdf", "Recibos y facturas PDF", INVOICE_PDFS, False),
        ("evidencias", "Evidencias de órdenes", ORDER_EVIDENCE, True),
        ("marca", "Archivos de marca", BRANDING_FILES, True),
        ("sesion_whatsapp", "Sesión de WhatsApp", WHATSAPP_SESSION, True),
        ("respaldos", "Respaldos cifrados", BACKUP_DIR, False),
    )
    scanned = await asyncio.gather(
        *(asyncio.to_thread(directory_stats, item[2]) for item in definitions)
    )
    for (key, label, _path, protected), stats in zip(definitions, scanned):
        categories.append({"clave": key, "nombre": label, "protegido": protected, **stats})

    db_rows = (
        await db.execute(
            text(
                "SELECT table_name, data_length + index_length AS bytes "
                "FROM information_schema.tables "
                "WHERE table_schema = DATABASE() ORDER BY bytes DESC"
            )
        )
    ).mappings().all()
    db_bytes = sum(int(row["bytes"] or 0) for row in db_rows)
    categories.append({
        "clave": "base_datos",
        "nombre": "Base de datos",
        "protegido": True,
        "archivos": len(db_rows),
        "bytes": db_bytes,
        "accesible": True,
    })

    counts = dict(
        (
            await db.execute(
                select(
                    ComprobantePagoRevisionModel.estado,
                    func.count(ComprobantePagoRevisionModel.id),
                ).group_by(ComprobantePagoRevisionModel.estado)
            )
        ).all()
    )
    archived = await db.scalar(
        select(func.count(ComprobantePagoRevisionModel.id)).where(
            ComprobantePagoRevisionModel.archivado_en.is_not(None)
        )
    )
    image_removed = await db.scalar(
        select(func.count(ComprobantePagoRevisionModel.id)).where(
            ComprobantePagoRevisionModel.archivo_eliminado_en.is_not(None)
        )
    )
    closures = (
        await db.scalars(
            select(CierreAlmacenamientoModel)
            .order_by(CierreAlmacenamientoModel.periodo.desc())
            .limit(24)
        )
    ).all()
    return {
        "disco": {
            "total_bytes": disk.total,
            "usado_bytes": disk.used,
            "libre_bytes": disk.free,
            "porcentaje_usado": round((disk.used / disk.total) * 100, 1) if disk.total else 0,
        },
        "categorias": sorted(categories, key=lambda item: item["bytes"], reverse=True),
        "tablas_principales": [
            {"nombre": row["table_name"], "bytes": int(row["bytes"] or 0)}
            for row in db_rows[:12]
        ],
        "comprobantes": {
            "pendientes": int(counts.get("pendiente", 0) + counts.get("procesando", 0)),
            "aprobados": int(counts.get("aprobado", 0)),
            "rechazados": int(counts.get("rechazado", 0)),
            "archivados": int(archived or 0),
            "imagenes_eliminadas": int(image_removed or 0),
        },
        "politica": policy_from_config(config),
        "cierres": [
            {
                "id": item.id,
                "periodo": item.periodo,
                "tipo": item.tipo,
                "aprobados": item.comprobantes_aprobados,
                "rechazados": item.comprobantes_rechazados,
                "pendientes": item.comprobantes_pendientes,
                "bytes_comprobantes": item.bytes_comprobantes,
                "bytes_liberados": item.bytes_liberados,
                "cerrado_en": item.cerrado_en,
            }
            for item in closures
        ],
    }


async def close_period(
    db: AsyncSession,
    config: ConfiguracionSistema,
    period: str,
    closure_type: str = "manual",
    user_id: int | None = None,
) -> dict:
    start, end = _period_bounds(period)
    if start > datetime.now():
        raise ValueError("No se puede cerrar un periodo futuro")
    revisions = (
        await db.scalars(
            select(ComprobantePagoRevisionModel).where(
                ComprobantePagoRevisionModel.fecha_recepcion >= start,
                ComprobantePagoRevisionModel.fecha_recepcion < end,
            )
        )
    ).all()
    counts = {"aprobado": 0, "rechazado": 0, "pendiente": 0}
    total_bytes = 0
    now = datetime.now()
    for revision in revisions:
        if revision.estado in {"aprobado", "rechazado"}:
            counts[revision.estado] += 1
            revision.archivado_en = revision.archivado_en or now
            path = _safe_file_from_url(revision.media_url, RECEIPT_UPLOADS)
            if path and path.is_file():
                try:
                    total_bytes += path.stat().st_size
                except OSError:
                    pass
        else:
            counts["pendiente"] += 1

    closure = await db.scalar(
        select(CierreAlmacenamientoModel).where(
            CierreAlmacenamientoModel.periodo == period
        )
    )
    if closure is None:
        closure = CierreAlmacenamientoModel(
            periodo=period,
            tipo=closure_type,
            cerrado_por_id=user_id,
            cerrado_en=now,
        )
        db.add(closure)
    closure.comprobantes_aprobados = counts["aprobado"]
    closure.comprobantes_rechazados = counts["rechazado"]
    closure.comprobantes_pendientes = counts["pendiente"]
    closure.bytes_comprobantes = max(int(closure.bytes_comprobantes or 0), total_bytes)
    config.ultimo_cierre_almacenamiento = period
    await db.commit()
    return {"periodo": period, **counts, "bytes_comprobantes": total_bytes}


async def cleanup_storage(db: AsyncSession, config: ConfiguracionSistema) -> dict:
    now = datetime.now()
    freed = 0
    removed_receipts = 0
    removed_pdfs = 0
    removed_temporary = 0
    freed_by_period: dict[str, int] = {}

    revisions = (
        await db.scalars(
            select(ComprobantePagoRevisionModel).where(
                ComprobantePagoRevisionModel.archivado_en.is_not(None),
                ComprobantePagoRevisionModel.archivo_eliminado_en.is_(None),
                ComprobantePagoRevisionModel.estado.in_(["aprobado", "rechazado"]),
            )
        )
    ).all()
    for revision in revisions:
        retention = (
            config.dias_retencion_comprobantes_aprobados
            if revision.estado == "aprobado"
            else config.dias_retencion_comprobantes_rechazados
        )
        if revision.fecha_recepcion > now - timedelta(days=retention or 90):
            continue
        path = _safe_file_from_url(revision.media_url, RECEIPT_UPLOADS)
        if path and path.is_file():
            try:
                size = path.stat().st_size
                path.unlink()
                freed += size
                period = revision.fecha_recepcion.strftime("%Y-%m")
                freed_by_period[period] = freed_by_period.get(period, 0) + size
                removed_receipts += 1
            except OSError:
                continue
        revision.archivo_eliminado_en = now

    pdf_cutoff = now - timedelta(days=config.dias_retencion_recibos_pdf or 180)
    messages = (
        await db.scalars(
            select(MensajeChatModel).where(
                MensajeChatModel.ruta_archivo.is_not(None),
                MensajeChatModel.fecha < pdf_cutoff,
                MensajeChatModel.estado_envio.not_in(["pendiente", "procesando"]),
            )
        )
    ).all()
    for message in messages:
        path = _safe_file_from_url(message.ruta_archivo, INVOICE_PDFS)
        if not path:
            continue
        if path.is_file():
            try:
                size = path.stat().st_size
                path.unlink()
                freed += size
                removed_pdfs += 1
            except OSError:
                continue
        message.ruta_archivo = None

    linked_names = {
        Path(urlparse(value).path).name
        for value in (
            await db.scalars(
                select(ComprobantePagoRevisionModel.media_url).where(
                    ComprobantePagoRevisionModel.archivo_eliminado_en.is_(None)
                )
            )
        ).all()
        if value
    }
    temporary_cutoff = now.timestamp() - (config.dias_retencion_archivos_whatsapp or 90) * 86400
    try:
        for path in RECEIPT_UPLOADS.iterdir():
            if not path.is_file() or path.is_symlink() or path.name in linked_names:
                continue
            try:
                if path.stat().st_mtime < temporary_cutoff:
                    size = path.stat().st_size
                    path.unlink()
                    freed += size
                    removed_temporary += 1
            except OSError:
                continue
    except OSError:
        pass

    if freed_by_period:
        closures = (
            await db.scalars(
                select(CierreAlmacenamientoModel).where(
                    CierreAlmacenamientoModel.periodo.in_(freed_by_period)
                )
            )
        ).all()
        for closure in closures:
            closure.bytes_liberados = int(closure.bytes_liberados or 0) + freed_by_period[closure.periodo]

    config.ultima_limpieza_almacenamiento = now
    await db.commit()
    return {
        "bytes_liberados": freed,
        "comprobantes_eliminados": removed_receipts,
        "pdf_eliminados": removed_pdfs,
        "temporales_eliminados": removed_temporary,
        "registros_antifraude_conservados": True,
    }


def update_backup_retention(days: int) -> bool:
    env_path = APP_ROOT / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
        key = "FDEZNET_BACKUP_RETENTION_DAYS="
        updated = [line for line in lines if not line.startswith(key)]
        updated.append(f"{key}{days}")
        temporary = env_path.with_name(".env.storage.tmp")
        temporary.write_text("\n".join(updated) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(env_path)
        return True
    except OSError:
        return False
