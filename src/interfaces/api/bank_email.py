from datetime import datetime
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.services.bank_email_service import (
    BankEmailError,
    BankEmailService,
    decrypt_mail_secret,
    encrypt_mail_secret,
)
from src.infrastructure.auth import role_required
from src.infrastructure.database import get_db
from src.infrastructure.models import TransaccionCorreoBancoModel


router = APIRouter(prefix="/correo-bancario", tags=["Correo bancario"])


class BankEmailConfigRequest(BaseModel):
    activo: bool = False
    auto_aprobar: bool = False
    correo: str = Field(min_length=5, max_length=160)
    password_aplicacion: Optional[str] = Field(default=None, max_length=100)
    remitente_permitido: str = Field(min_length=5, max_length=255)
    asunto_filtro: Optional[str] = Field(default=None, max_length=255)
    carpeta: str = Field(default="INBOX", min_length=1, max_length=100)
    ventana_dias: int = Field(default=3, ge=1, le=30)
    tolerancia_monto: Decimal = Field(default=Decimal("0.00"), ge=0, le=100)
    requiere_dkim: bool = True

    @field_validator("correo")
    @classmethod
    def validate_email(cls, value: str) -> str:
        value = value.strip().casefold()
        if "@" not in value or value.startswith("@") or value.endswith("@"):
            raise ValueError("Correo de Gmail inválido")
        return value

    @field_validator("remitente_permitido")
    @classmethod
    def validate_senders(cls, value: str) -> str:
        senders = [item.strip().casefold() for item in value.split(",") if item.strip()]
        if not senders or any("@" not in item for item in senders):
            raise ValueError("Indica uno o más remitentes bancarios válidos")
        return ",".join(dict.fromkeys(senders))


class BankEmailTestRequest(BaseModel):
    correo: str = Field(min_length=5, max_length=160)
    password_aplicacion: Optional[str] = Field(default=None, max_length=100)
    carpeta: str = Field(default="INBOX", min_length=1, max_length=100)


def _response(config) -> dict:
    return {
        "activo": config.activo,
        "auto_aprobar": config.auto_aprobar,
        "proveedor": config.proveedor,
        "correo": config.correo,
        "credencial_configurada": bool(config.secreto_cifrado),
        "credencial_verificada_en": config.credencial_verificada_en,
        "remitente_permitido": config.remitente_permitido,
        "asunto_filtro": config.asunto_filtro,
        "carpeta": config.carpeta,
        "ventana_dias": config.ventana_dias,
        "tolerancia_monto": config.tolerancia_monto,
        "requiere_dkim": config.requiere_dkim,
        "ultima_revision": config.ultima_revision,
        "ultimo_error": config.ultimo_error,
    }


@router.get("/configuracion")
async def get_configuration(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    config = await BankEmailService().get_config(db)
    await db.commit()
    return _response(config)


@router.post("/configuracion")
async def save_configuration(
    data: BankEmailConfigRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin"])),
):
    config = await BankEmailService().get_config(db)
    previous_address = config.correo
    if data.password_aplicacion:
        try:
            config.secreto_cifrado = encrypt_mail_secret(data.password_aplicacion)
        except BankEmailError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        config.credencial_verificada_en = None
    if previous_address and previous_address.casefold() != data.correo.casefold():
        config.credencial_verificada_en = None
        config.ultimo_uid = None
    if data.activo and (not config.secreto_cifrado or not config.credencial_verificada_en):
        raise HTTPException(
            status_code=400,
            detail="Guarda y prueba primero la conexión con Gmail antes de activarla",
        )
    if data.auto_aprobar and not data.requiere_dkim:
        raise HTTPException(
            status_code=400,
            detail="La aprobación automática exige validar DKIM y DMARC",
        )
    config.activo = data.activo
    config.auto_aprobar = data.auto_aprobar
    config.correo = data.correo
    config.remitente_permitido = data.remitente_permitido
    config.asunto_filtro = (data.asunto_filtro or "").strip() or None
    config.carpeta = data.carpeta.strip()
    config.ventana_dias = data.ventana_dias
    config.tolerancia_monto = data.tolerancia_monto
    config.requiere_dkim = data.requiere_dkim
    config.ultimo_error = None
    await db.commit()
    await db.refresh(config)
    return _response(config)


@router.post("/probar")
async def test_configuration(
    data: BankEmailTestRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin"])),
):
    config = await BankEmailService().get_config(db)
    password = data.password_aplicacion
    if not password and config.secreto_cifrado:
        try:
            password = decrypt_mail_secret(config.secreto_cifrado)
        except BankEmailError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not password:
        raise HTTPException(status_code=400, detail="Indica la contraseña de aplicación")
    try:
        await BankEmailService().test_connection(
            address=data.correo.strip().casefold(),
            app_password=password,
            folder=data.carpeta.strip(),
        )
    except BankEmailError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    using_saved_credential = (
        not data.password_aplicacion
        and bool(config.secreto_cifrado)
        and data.correo.strip().casefold() == (config.correo or "").casefold()
    )
    if using_saved_credential:
        config.credencial_verificada_en = datetime.utcnow()
        config.ultimo_error = None
        await db.commit()
    return {"status": "ok", "mensaje": "Conexión de solo lectura con Gmail verificada"}


@router.post("/sincronizar")
async def synchronize(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    try:
        service = BankEmailService()
        imported = await service.sync(db)
        reconciled = await service.reconcile_pending(db)
        return {**imported, "conciliacion": reconciled}
    except BankEmailError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/transacciones")
async def list_transactions(
    estado: str = Query(
        default="todos",
        pattern="^(todos|disponible|procesando|conciliada|ignorada)$",
    ),
    limite: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor", "cajero"])),
):
    filters = [] if estado == "todos" else [TransaccionCorreoBancoModel.estado == estado]
    total = await db.scalar(
        select(func.count(TransaccionCorreoBancoModel.id)).where(*filters)
    )
    items = (
        await db.execute(
            select(TransaccionCorreoBancoModel)
            .where(*filters)
            .order_by(TransaccionCorreoBancoModel.fecha_correo.desc())
            .limit(limite)
        )
    ).scalars().all()
    return {
        "total": total or 0,
        "items": [
            {
                "id": item.id,
                "remitente": item.remitente,
                "asunto": item.asunto,
                "fecha_correo": item.fecha_correo,
                "monto": item.monto,
                "referencia": item.referencia,
                "concepto": item.concepto,
                "autenticado": item.autenticado,
                "detalle_autenticacion": item.detalle_autenticacion,
                "estado": item.estado,
                "pago_id": item.pago_id,
            }
            for item in items
        ],
    }
