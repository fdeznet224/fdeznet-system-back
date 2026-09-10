import os
import re
import httpx
import logging
import unicodedata
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import List, Optional
from uuid import uuid4
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Depends, Request, WebSocket, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select, func, cast, String
from sqlalchemy.orm import joinedload, selectinload
from sqlalchemy.ext.asyncio import AsyncSession

# Importaciones de Infraestructura y Modelos
from src.infrastructure.database import SessionLocal, get_db
from src.infrastructure.auth import (
    decode_access_token,
    role_required,
    verify_webhook_secret,
)
from src.infrastructure.models import (
    ClienteModel, 
    MensajeChatModel, 
    PagoAutovalidadoModel, 
    FacturaModel, 
    UsuarioModel,
    ConfiguracionModel,
    ConfiguracionSistema,  # 👈 Añadido para poder alertarte de fraudes
    PlantillaMensajeModel,
    ComprobantePagoRevisionModel,
    TransaccionCorreoBancoModel,
)
from src.infrastructure.whatsapp_client import (
    GLOBAL_SETTINGS,
    WhatsAppService,
    set_intervalo_default,
    whatsapp_queue,
)
from src.infrastructure.socket_manager import manager

logger = logging.getLogger(__name__)

# Importaciones de Servicios
from src.application.services.ocr_service import OCRService
from src.application.services.bank_email_service import (
    BankEmailError,
    BankEmailService,
    normalize_reference,
    normalize_reference_for_match,
)
from src.application.services.billing_service import BillingService
from src.application.services.finance_service import FinanceService
from src.application.services.access_control_service import (
    verificar_acceso_cliente,
)
from src.application.services.whatsapp_outbox_service import (
    ESTADOS_SALIDA,
    WhatsAppOutboxService,
)

router = APIRouter(prefix="/whatsapp", tags=["Configuración WhatsApp"])
webhook_router = APIRouter(prefix="/whatsapp", tags=["Webhooks WhatsApp"])

# --- CONFIGURACIÓN Y MEMORIA GLOBAL ---
BASE_NODE_URL = os.getenv("WHATSAPP_BASE_URL") or (
    "http://whatsapp:3000"
    if os.environ.get("ENVIRONMENT") == "production"
    else "http://127.0.0.1:3000"
)
NODE_HEADERS = {
    "X-Webhook-Secret": os.getenv("WEBHOOK_SECRET", ""),
}

# Memoria temporal para el Bot (Estado por número de teléfono)
bot_memory = {}
ocr_tool = OCRService()
BOT_KEYWORD = "fdezbot"
BOT_SESSION_MINUTES = 15


def mensaje_comprobante_ya_recibido(
    estado: str | None,
    *,
    pago_registrado: bool = False,
) -> str:
    if pago_registrado or estado == "aprobado":
        return (
            "✅ Este comprobante ya fue aprobado y registrado anteriormente. "
            "No se aplicará el pago otra vez."
        )
    if estado in {"pendiente", "procesando"}:
        return (
            "⏳ Este comprobante ya fue recibido y continúa en revisión. "
            "No es necesario enviarlo nuevamente."
        )
    return (
        "⚠️ Este comprobante ya fue revisado anteriormente y no puede "
        "registrarse otra vez. Si necesitas una aclaración, contacta a soporte."
    )


def referencia_canonica_sql(columna):
    return func.replace(func.replace(func.upper(columna), "O", "0"), "I", "1")


def esta_fuera_de_horario(ahora: datetime | None = None) -> bool:
    """Horario de atención de FdezNet en America/Mexico_City."""
    from datetime import time
    from zoneinfo import ZoneInfo

    zona = ZoneInfo("America/Mexico_City")
    local = ahora.astimezone(zona) if ahora and ahora.tzinfo else (
        ahora or datetime.now(zona)
    )
    if local.weekday() <= 4:
        return not (time(8, 0) <= local.time() < time(20, 0))
    if local.weekday() == 5:
        return not (time(9, 0) <= local.time() < time(14, 0))
    return True


def mensaje_fuera_de_horario(
    empresa_nombre: str = "FdezNet",
    asistente_nombre: str = "FdezBot",
) -> str:
    return (
        "🌙 *Estamos fuera del horario de atención.*\n\n"
        "Nuestro horario es:\n"
        "• Lunes a viernes: 8:00 a. m. a 8:00 p. m.\n"
        "• Sábado: 9:00 a. m. a 2:00 p. m.\n"
        "• Domingo: cerrado.\n\n"
        f"🤖 Soy *{asistente_nombre}*, el asistente automático de "
        f"{empresa_nombre}. Durante este "
        "horario voy a atenderte y puedo ayudarte con pagos, saldo, promesas "
        "y problemas de conexión."
    )


def mensaje_audio_no_disponible(asistente_nombre: str = "FdezBot") -> str:
    return (
        f"🎤 Por el momento {asistente_nombre} no procesa notas de voz. "
        "Por favor escribe tu solicitud o envía *fdezbot* para usar el menú."
    )


def construir_menu_bot(asistente_nombre: str = "FdezBot") -> str:
    return (
        f"🤖 *Bienvenido a {asistente_nombre}*\n"
        "Soy tu asistente de pagos y servicios. Elige una opción:\n\n"
        "1️⃣ *Reportar pago* (transferencia o depósito)\n"
        "2️⃣ *Promesa de pago* (puede reactivar tu servicio)\n"
        "3️⃣ *Consultar mi servicio y saldo*\n"
        "4️⃣ *Datos para depósito o transferencia*\n"
        "5️⃣ *No tengo internet*\n\n"
        "👉 Responde con un número del *1* al *5*.\n"
        "Escribe *menú* para volver aquí o *cancelar* para salir."
    )


def normalizar_texto_bot(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", (texto or "").lower())
    return "".join(
        caracter
        for caracter in texto
        if not unicodedata.combining(caracter)
    )


def detectar_intencion_bot(
    texto: str,
    *,
    es_comprobante: bool = False,
) -> str:
    """Clasifica por reglas las solicitudes más comunes del ISP."""
    if es_comprobante:
        return "pago"
    limpio = normalizar_texto_bot(texto)
    palabras = set(re.findall(r"[a-z0-9]+", limpio))
    if any(
        frase in limpio
        for frase in (
            "numero de cuenta",
            "a que cuenta",
            "donde deposito",
            "donde transfiero",
        )
    ):
        return "datos_pago"
    if palabras & {
        "cuenta",
        "clabe",
        "deposito",
        "depositar",
        "transferencia",
        "transferir",
    }:
        return "datos_pago"
    if any(
        frase in limpio
        for frase in (
            "no tengo internet",
            "sin internet",
            "no hay internet",
            "no tengo servicio",
        )
    ):
        return "sin_internet"
    if palabras & {"reactivar", "reactivacion", "reconexion", "activar"}:
        return "promesa"
    if palabras & {"saldo", "debo", "deuda", "vencimiento", "servicio"}:
        return "estado"
    if palabras & {"pague", "pago", "comprobante", "ticket"}:
        return "pago"
    if palabras & {"promesa", "prorroga"}:
        return "promesa"
    return "menu"


async def obtener_datos_pago(db: AsyncSession) -> str:
    plantilla = (
        await db.execute(
            select(PlantillaMensajeModel).where(
                PlantillaMensajeModel.tipo == "datos_pago",
                PlantillaMensajeModel.activo.is_(True),
            )
        )
    ).scalar_one_or_none()
    if plantilla and plantilla.texto.strip():
        return "🏦 *Datos para realizar tu pago*\n\n" + plantilla.texto.strip()
    return (
        "🏦 Los datos bancarios todavía no están publicados en el "
        "autoservicio. Tu solicitud quedó registrada y un asesor te enviará "
        "la cuenta correcta en el próximo horario de atención."
    )


def interpretar_fecha_promesa(texto: str, hoy: date | None = None) -> date:
    hoy = hoy or date.today()
    limpio = texto.strip()
    if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", limpio):
        return datetime.strptime(limpio, "%d/%m/%Y").date()
    dia = int(limpio)
    if dia < 1 or dia > 31:
        raise ValueError("Escribe un día válido o una fecha DD/MM/AAAA")
    if dia <= hoy.day:
        mes = hoy.month + 1 if hoy.month < 12 else 1
        ano = hoy.year if hoy.month < 12 else hoy.year + 1
    else:
        mes, ano = hoy.month, hoy.year
    try:
        return date(ano, mes, dia)
    except ValueError as exc:
        raise ValueError(
            "Ese día no existe en el mes indicado; usa DD/MM/AAAA"
        ) from exc


async def obtener_factura_cobrable(db: AsyncSession, cliente_id: int):
    """Obtiene la deuda más antigua, incluso si el cliente ya fue cortado."""
    stmt = (
        select(FacturaModel)
        .where(
            FacturaModel.cliente_id == cliente_id,
            FacturaModel.estado.in_(["pendiente", "vencida"]),
            FacturaModel.saldo_pendiente > 0,
        )
        .order_by(FacturaModel.fecha_vencimiento.asc())
    )
    factura = (await db.execute(stmt)).scalars().first()
    if not factura:
        return None
    try:
        cobrable, _, _ = await BillingService(
            db
        ).preparar_factura_cobrable(
            factura.id,
            fecha_reactivacion=date.today(),
        )
    except ValueError:
        # La preparación puede haber cerrado todos los ciclos como sin cargo.
        await db.commit()
        return None
    await db.commit()
    return cobrable


# --- SCHEMAS ---
class Destinatario(BaseModel):
    numero: str = Field(min_length=10, max_length=100)
    nombre: str = Field(min_length=1, max_length=150)

class CampanaMasiva(BaseModel):
    clientes: Optional[List[Destinatario]] = None
    zona_id: Optional[int] = Field(default=None, gt=0)
    router_id: Optional[int] = Field(default=None, gt=0)
    mensaje: str = Field(min_length=1, max_length=10000)
    ruta_archivo: Optional[str] = None
    intervalo_segundos: int = Field(default=0, ge=0, le=3600)

    @model_validator(mode="after")
    def validar_destino(self):
        if not self.clientes and not self.zona_id and not self.router_id:
            raise ValueError("Selecciona una zona o router para la campaña")
        if self.clientes and (self.zona_id or self.router_id):
            raise ValueError("Usa clientes o un filtro de zona/router, no ambos")
        return self

class MensajeEnviarRequest(BaseModel):
    mensaje: str = Field(min_length=1, max_length=10000)

class AckWebhookRequest(BaseModel):
    wa_id: Optional[str] = None
    mensaje_chat_id: Optional[int] = None
    ack: int = Field(ge=-1, le=4)

    @model_validator(mode="after")
    def validar_identificador(self):
        if not self.wa_id and not self.mensaje_chat_id:
            raise ValueError("Indica wa_id o mensaje_chat_id")
        return self


class ReintentoMasivoRequest(BaseModel):
    ids: Optional[list[int]] = None
    limite: int = Field(default=100, ge=1, le=500)


class AprobarComprobanteRequest(BaseModel):
    cliente_id: int = Field(gt=0)
    factura_id: Optional[int] = Field(default=None, gt=0)
    monto: Optional[Decimal] = Field(
        default=None,
        gt=0,
        max_digits=12,
        decimal_places=2,
    )
    referencia: Optional[str] = Field(default=None, max_length=100)
    notas: Optional[str] = Field(default=None, max_length=1000)


class RechazarComprobanteRequest(BaseModel):
    motivo: str = Field(min_length=3, max_length=1000)


class ConfiguracionWhatsAppRequest(BaseModel):
    intervalo_segundos: int = Field(ge=1, le=3600)


def renderizar_mensaje_campana(
    plantilla: str,
    *,
    nombre: str,
    numero: str,
    cliente: Optional[ClienteModel] = None,
):
    """Reemplaza variables comerciales sin dejar placeholders desconocidos."""
    partes = nombre.strip().split()
    ahora = datetime.now()
    variables = {
        "nombre": nombre,
        "cliente": nombre,
        "nombre_completo": nombre,
        "apellido": " ".join(partes[1:]) if len(partes) > 1 else "",
        "telefono": numero or "",
        "cedula": getattr(cliente, "cedula", None) or "",
        "contrato": getattr(cliente, "cedula", None) or "",
        "zona": (
            cliente.zona.nombre
            if cliente and cliente.zona
            else ""
        ),
        "router": (
            cliente.router.nombre
            if cliente and cliente.router
            else ""
        ),
        "plan": (
            cliente.plan.nombre
            if cliente and cliente.plan
            else ""
        ),
        "fecha": ahora.strftime("%d/%m/%Y"),
        "hora": ahora.strftime("%H:%M"),
        "dia": ahora.strftime("%d"),
        "mes": ahora.strftime("%m"),
        "ano": ahora.strftime("%Y"),
    }
    return re.sub(
        r"\{([a-z_]+)\}",
        lambda match: str(variables.get(match.group(1), match.group(0))),
        plantilla,
    )

# ==========================================
# ⚙️ FUNCION AUXILIAR: VALIDACIÓN FINAL (CAPA 3)
# ==========================================
async def procesar_validacion_final_pago(mensaje_texto, estado, telefono_raw, db, wa_service):
    cedula_input = mensaje_texto.upper().strip()
    cliente_final = await db.get(ClienteModel, estado["cliente_id"])

    # Verificación de firma: debe escribir su número de contrato exacto.
    if cedula_input != "SI" and cedula_input != cliente_final.cedula:
        await wa_service.enviar_mensaje(telefono=telefono_raw, mensaje="❌ Número de contrato incorrecto. Inténtalo de nuevo o escribe 'cancelar'.")
        return {"status": "firma_invalida"}

    factura = await obtener_factura_cobrable(db, cliente_final.id)

    if not factura:
        res = "✅ Identidad confirmada, pero no tienes facturas pendientes de pago en este momento."
        del bot_memory[telefono_raw]
    else:
        # 🛡️ CAPA 3: Validación Matemática de Montos
        monto_ticket = FinanceService.dinero(estado["pago"]["monto"])
        deuda_real = Decimal(factura.saldo_pendiente or 0)

        if monto_ticket < deuda_real:
            revision_id = estado.get("revision_id")
            revision = (
                await db.get(ComprobantePagoRevisionModel, revision_id)
                if revision_id
                else None
            )
            if revision:
                revision.motivo_revision = "monto_no_coincide"
                revision.notas_revision = (
                    f"OCR ${monto_ticket}; deuda ${deuda_real}"
                )
                await db.commit()
            res = f"❌ *Pago Rechazado*\n\nEl comprobante indica un pago de *${monto_ticket}*, pero tu factura es de *${deuda_real}*.\n\nSi es un error de lectura, envía una foto más clara o contacta a soporte."
            del bot_memory[telefono_raw]
        else:
            revision_id = estado.get("revision_id")
            revision = (
                await db.get(ComprobantePagoRevisionModel, revision_id)
                if revision_id
                else None
            )
            try:
                if not revision:
                    raise RuntimeError("No existe el comprobante para conciliar")
                revision.cliente_id = cliente_final.id
                revision.factura_id = factura.id
                await db.commit()
                correo_service = BankEmailService()
                try:
                    await correo_service.sync(db)
                except BankEmailError as exc:
                    revision = await db.get(
                        ComprobantePagoRevisionModel,
                        revision_id,
                    )
                    revision.motivo_revision = "error_sincronizacion_correo"
                    revision.notas_revision = str(exc)[:1000]
                    await db.commit()
                revision = await db.get(ComprobantePagoRevisionModel, revision_id)
                resultado_pago = await correo_service.reconcile_revision(
                    db,
                    revision,
                )

                if resultado_pago.get("approved"):
                    if resultado_pago.get("reactivado"):
                        estado_servicio = (
                            "\nTu servicio fue reactivado y MikroTik "
                            "confirmó el cambio. 🚀"
                        )
                    elif cliente_final.estado == "suspendido":
                        estado_servicio = (
                            "\nEl pago fue aplicado, pero todavía existe otra "
                            "deuda pendiente; el servicio continúa suspendido."
                        )
                    else:
                        estado_servicio = "\nTu servicio permanece activo."
                    res = (
                        f"✅ ¡Pago confirmado por el banco, {cliente_final.nombre}!\n"
                        f"La transferencia de *${monto_ticket}* fue conciliada "
                        f"con el correo bancario.{estado_servicio}"
                    )
                else:
                    status = resultado_pago.get("status")
                    if status == "correo_confirmado_revision_manual":
                        res = (
                            "✅ Encontré la transferencia en el correo bancario. "
                            "El pago quedó listo para aprobación del administrador."
                        )
                    elif status == "correo_bancario_no_configurado":
                        res = (
                            "📨 Guardé tu comprobante, pero la validación por "
                            "correo bancario aún no está configurada. Un asesor "
                            "revisará el pago."
                        )
                    else:
                        res = (
                            "⏳ Recibí tu comprobante, pero todavía no encuentro "
                            "una transferencia bancaria con la misma referencia y "
                            "monto. Seguiré revisando el correo; no se aplicó ningún "
                            "pago por ahora."
                        )
                del bot_memory[telefono_raw]
            except Exception as e:
                logger.exception("Error conciliando pago del bot con correo bancario")
                await db.rollback()
                revision = (
                    await db.get(ComprobantePagoRevisionModel, revision_id)
                    if revision_id
                    else None
                )
                if revision:
                    revision.estado = "pendiente"
                    revision.motivo_revision = "error_conciliacion_correo"
                    revision.notas_revision = str(e)[:1000]
                    await db.commit()
                res = (
                    "⚠️ Tu comprobante quedó guardado para revisión, pero no "
                    "pude consultar el correo bancario en este momento. No se "
                    "registró ningún pago."
                )
                if telefono_raw in bot_memory:
                    del bot_memory[telefono_raw]
    
    await wa_service.enviar_mensaje(telefono=telefono_raw, mensaje=res)
    return {"status": "bot_pago_finished"}


# ==========================================
# ⚙️ ENDPOINTS DE CONFIGURACIÓN Y CAMPAÑAS
# ==========================================
@router.get("/configuracion")
async def get_config(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    valor = (
        await db.execute(
            select(ConfiguracionModel.valor).where(
                ConfiguracionModel.clave
                == "whatsapp_intervalo_segundos"
            )
        )
    ).scalar_one_or_none()
    if valor:
        try:
            set_intervalo_default(int(valor))
        except (TypeError, ValueError):
            pass
    return GLOBAL_SETTINGS

@router.post("/configuracion")
async def set_config(
    datos: ConfiguracionWhatsAppRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    intervalo = set_intervalo_default(
        datos.intervalo_segundos
    )
    configuracion = (
        await db.execute(
            select(ConfiguracionModel).where(
                ConfiguracionModel.clave
                == "whatsapp_intervalo_segundos"
            )
        )
    ).scalar_one_or_none()
    if configuracion:
        configuracion.valor = str(intervalo)
    else:
        db.add(
            ConfiguracionModel(
                clave="whatsapp_intervalo_segundos",
                valor=str(intervalo),
            )
        )
    await db.commit()
    return {"status": "ok", "intervalo": intervalo}


async def _factura_sugerida_revision(
    db: AsyncSession,
    cliente_id: Optional[int],
):
    if not cliente_id:
        return None
    return (
        await db.execute(
            select(FacturaModel)
            .where(
                FacturaModel.cliente_id == cliente_id,
                FacturaModel.estado.in_(["pendiente", "vencida"]),
                FacturaModel.saldo_pendiente > 0,
            )
            .order_by(FacturaModel.fecha_vencimiento.asc())
            .limit(1)
        )
    ).scalars().first()


@router.get("/comprobantes-revision")
async def listar_comprobantes_revision(
    estado: str = Query(default="pendiente"),
    pagina: int = Query(default=1, ge=1),
    limite: int = Query(default=30, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor", "cajero"])),
):
    estados = {"pendiente", "procesando", "aprobado", "rechazado", "todos"}
    if estado not in estados:
        raise HTTPException(status_code=400, detail="Estado de revisión inválido")
    filtros = [] if estado == "todos" else [ComprobantePagoRevisionModel.estado == estado]
    total = (
        await db.execute(
            select(func.count(ComprobantePagoRevisionModel.id)).where(*filtros)
        )
    ).scalar_one()
    registros = (
        await db.execute(
            select(ComprobantePagoRevisionModel)
            .options(
                joinedload(ComprobantePagoRevisionModel.cliente),
                joinedload(ComprobantePagoRevisionModel.factura),
                joinedload(ComprobantePagoRevisionModel.revisado_por),
                joinedload(ComprobantePagoRevisionModel.transaccion_correo),
            )
            .where(*filtros)
            .order_by(ComprobantePagoRevisionModel.fecha_recepcion.desc())
            .offset((pagina - 1) * limite)
            .limit(limite)
        )
    ).scalars().all()
    items = []
    for item in registros:
        factura = item.factura or await _factura_sugerida_revision(
            db,
            item.cliente_id,
        )
        items.append({
            "id": item.id,
            "estado": item.estado,
            "cliente_id": item.cliente_id,
            "cliente_nombre": item.cliente.nombre if item.cliente else None,
            "cliente_cedula": item.cliente.cedula if item.cliente else None,
            "telefono": item.telefono,
            "monto_detectado": item.monto_detectado,
            "folio_detectado": item.folio_detectado,
            "cedula_detectada": item.cedula_detectada,
            "motivo_revision": item.motivo_revision,
            "notas_revision": item.notas_revision,
            "fecha_recepcion": item.fecha_recepcion,
            "fecha_revision": item.fecha_revision,
            "revisado_por": (
                item.revisado_por.nombre_completo
                if item.revisado_por
                else None
            ),
            "pago_id": item.pago_id,
            "validacion_correo": (
                {
                    "id": item.transaccion_correo.id,
                    "fecha": item.transaccion_correo.fecha_correo,
                    "monto": item.transaccion_correo.monto,
                    "referencia": item.transaccion_correo.referencia,
                    "autenticado": item.transaccion_correo.autenticado,
                    "estado": item.transaccion_correo.estado,
                }
                if item.transaccion_correo
                else None
            ),
            "factura_sugerida": (
                {
                    "id": factura.id,
                    "saldo_pendiente": factura.saldo_pendiente,
                    "fecha_vencimiento": factura.fecha_vencimiento,
                }
                if factura
                else None
            ),
            "archivo_url": f"/whatsapp/comprobantes-revision/{item.id}/archivo",
        })
    return {
        "items": items,
        "total": total,
        "pagina": pagina,
        "limite": limite,
    }


@router.get("/comprobantes-revision/{comprobante_id}/archivo")
async def ver_archivo_comprobante(
    comprobante_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor", "cajero"])),
):
    comprobante = await db.get(ComprobantePagoRevisionModel, comprobante_id)
    if not comprobante:
        raise HTTPException(status_code=404, detail="Comprobante no encontrado")
    nombre = Path(urlparse(comprobante.media_url).path).name
    if not nombre or Path(nombre).suffix.lower() not in {
        ".jpg", ".jpeg", ".png", ".webp",
    }:
        raise HTTPException(status_code=404, detail="Archivo no disponible")
    uploads = Path(__file__).resolve().parents[3] / "bot_whatsapp" / "uploads"
    archivo = (uploads / nombre).resolve()
    if archivo.parent != uploads.resolve() or not archivo.is_file():
        raise HTTPException(status_code=404, detail="Archivo no disponible")
    return FileResponse(archivo)


@router.post("/comprobantes-revision/{comprobante_id}/aprobar")
async def aprobar_comprobante_revision(
    comprobante_id: int,
    datos: AprobarComprobanteRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor", "cajero"])),
):
    comprobante = (
        await db.execute(
            select(ComprobantePagoRevisionModel)
            .where(ComprobantePagoRevisionModel.id == comprobante_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not comprobante:
        raise HTTPException(status_code=404, detail="Comprobante no encontrado")
    if comprobante.estado in {"aprobado", "rechazado"}:
        raise HTTPException(status_code=409, detail="El comprobante ya fue revisado")

    correo_service = BankEmailService()
    config_correo = await correo_service.get_config(db)
    transaction = None
    if comprobante.transaccion_correo_id:
        transaction = (
            await db.execute(
                select(TransaccionCorreoBancoModel)
                .where(
                    TransaccionCorreoBancoModel.id
                    == comprobante.transaccion_correo_id
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
    if transaction is None:
        transaction, _reason = await correo_service.find_match(db, comprobante)
    if transaction is None or correo_service.transaction_match_reason(
        config_correo,
        comprobante,
        transaction,
    ) != "coincidencia_exacta":
        raise HTTPException(
            status_code=409,
            detail=(
                "No se puede aprobar: falta una transferencia bancaria "
                "autenticada que coincida en folio, monto, fecha y hora"
            ),
        )
    if transaction.pago_id:
        raise HTTPException(
            status_code=409,
            detail="La transferencia bancaria ya fue utilizada en otro pago",
        )
    monto = transaction.monto
    factura = (
        await db.get(FacturaModel, datos.factura_id)
        if datos.factura_id
        else await _factura_sugerida_revision(db, datos.cliente_id)
    )
    if not factura or factura.cliente_id != datos.cliente_id:
        raise HTTPException(
            status_code=400,
            detail="No se encontró una factura pendiente del cliente",
        )

    comprobante.estado = "procesando"
    comprobante.cliente_id = datos.cliente_id
    comprobante.factura_id = factura.id
    comprobante.transaccion_correo_id = transaction.id
    comprobante.revisado_por_id = current_user.id
    comprobante.notas_revision = datos.notas
    transaction.estado = "procesando"
    await db.commit()
    try:
        resultado = await BillingService(db).registrar_pago_completo(
            factura_id=factura.id,
            usuario_operador=current_user,
            metodo_pago="transferencia",
            monto=monto,
            referencia=transaction.referencia,
            clave_idempotencia=f"comprobante-revision:{comprobante.id}",
        )
        await db.refresh(comprobante)
        comprobante.estado = "aprobado"
        comprobante.pago_id = resultado.get("pago_id")
        comprobante.fecha_revision = datetime.now()
        if transaction:
            transaction.estado = "conciliada"
            transaction.pago_id = resultado.get("pago_id")
            transaction.conciliada_en = datetime.now()
        await db.commit()
        return {"status": "aprobado", **resultado}
    except ValueError as exc:
        await db.rollback()
        comprobante = await db.get(ComprobantePagoRevisionModel, comprobante_id)
        comprobante.estado = "pendiente"
        comprobante.notas_revision = f"Error al aprobar: {exc}"
        transaction = await db.get(TransaccionCorreoBancoModel, transaction.id)
        if transaction and not transaction.pago_id:
            transaction.estado = "disponible"
        await db.commit()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/comprobantes-revision/{comprobante_id}/rechazar")
async def rechazar_comprobante_revision(
    comprobante_id: int,
    datos: RechazarComprobanteRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor", "cajero"])),
):
    comprobante = (
        await db.execute(
            select(ComprobantePagoRevisionModel)
            .where(ComprobantePagoRevisionModel.id == comprobante_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not comprobante:
        raise HTTPException(status_code=404, detail="Comprobante no encontrado")
    if comprobante.estado not in {"pendiente", "procesando"}:
        raise HTTPException(status_code=409, detail="El comprobante ya fue revisado")
    comprobante.estado = "rechazado"
    comprobante.notas_revision = datos.motivo
    comprobante.revisado_por_id = current_user.id
    comprobante.fecha_revision = datetime.now()
    await db.commit()
    try:
        await WhatsAppService().enviar_mensaje(
            telefono=comprobante.telefono,
            mensaje=(
                "❌ No pudimos aprobar el comprobante enviado.\n"
                f"Motivo: {datos.motivo}\n\n"
                "Puedes enviar una nueva imagen clara o comunicarte con un asesor."
            ),
            tipo_evento="comprobante_rechazado",
        )
    except Exception:
        logger.exception(
            "No fue posible notificar rechazo del comprobante %s",
            comprobante.id,
        )
    return {"status": "rechazado"}

@router.post("/enviar-campana")
async def enviar_campana(
    datos: CampanaMasiva,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    intervalo_final = (
        datos.intervalo_segundos
        if datos.intervalo_segundos > 0
        else GLOBAL_SETTINGS["intervalo_default"]
    )
    lote_id = str(uuid4())
    registros = []

    if datos.clientes:
        destinatarios = [
            (None, cliente.numero, cliente.nombre, None)
            for cliente in datos.clientes
        ]
    else:
        allowed_router_ids = (
            [router.id for router in current_user.routers_asignados]
            if current_user.rol != "admin"
            else None
        )
        if allowed_router_ids == []:
            raise HTTPException(
                status_code=403,
                detail="No tienes routers asignados para esta campaña",
            )
        if (
            datos.router_id
            and allowed_router_ids is not None
            and datos.router_id not in allowed_router_ids
        ):
            raise HTTPException(
                status_code=403,
                detail="No tienes acceso a ese router",
            )

        filtros = [
            ClienteModel.telefono.isnot(None),
            ClienteModel.telefono != "",
        ]
        if datos.zona_id:
            filtros.append(ClienteModel.zona_id == datos.zona_id)
        if datos.router_id:
            filtros.append(ClienteModel.router_id == datos.router_id)
        if allowed_router_ids is not None:
            filtros.append(ClienteModel.router_id.in_(allowed_router_ids))
        clientes_db = (
            await db.execute(
                select(ClienteModel)
                .options(
                    selectinload(ClienteModel.zona),
                    selectinload(ClienteModel.router),
                    selectinload(ClienteModel.plan),
                )
                .where(*filtros)
                .order_by(ClienteModel.id.asc())
            )
        ).scalars().all()
        if not clientes_db:
            raise HTTPException(
                status_code=404,
                detail="No hay clientes con teléfono en el filtro seleccionado",
            )
        destinatarios = [
            (cliente.id, cliente.telefono, cliente.nombre, cliente)
            for cliente in clientes_db
        ]

    for cliente_id, numero, nombre, cliente_obj in destinatarios:
        texto = renderizar_mensaje_campana(
            datos.mensaje,
            nombre=nombre,
            numero=numero,
            cliente=cliente_obj,
        )
        registro = MensajeChatModel(
            cliente_id=cliente_id,
            telefono=whatsapp_queue.service._formatear_numero(
                numero
            ),
            direccion="salida",
            mensaje=texto,
            tipo_mensaje=(
                "documento" if datos.ruta_archivo else "texto"
            ),
            tipo_evento="campana",
            leido=True,
            ack=0,
            estado_envio="pendiente",
            ruta_archivo=datos.ruta_archivo,
            lote_id=lote_id,
            creado_por_id=current_user.id,
            intervalo_salida=intervalo_final,
        )
        db.add(registro)
        registros.append(registro)
    await db.commit()
    for registro in registros:
        await whatsapp_queue.agregar_tarea(
            {
                "mensaje_chat_id": registro.id,
                "intervalo": intervalo_final,
            }
        )
    return {
        "status": "procesando",
        "lote_id": lote_id,
        "total_mensajes": len(registros),
        "intervalo_segundos": intervalo_final,
    }


# ==========================================
# 🤖 WEBHOOK PRINCIPAL (EL CEREBRO DEL BOT Y CHAT)
# ==========================================
@webhook_router.post("/webhook/recibir")
async def webhook_recibir_mensaje(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_webhook_secret),
):
    datos = await request.json()
    
    telefono_busqueda = datos.get("telefono", "").strip() 
    telefono_raw = datos.get("telefono_raw", telefono_busqueda).strip() 
    
    mensaje_texto = datos.get("mensaje", "").strip()
    media_url = datos.get("mediaUrl")

    if not telefono_raw: return {"status": "ignorado"}
    texto_limpio = mensaje_texto.lower().strip()

    # 🔥 CARRIL RÁPIDO PARA BOT Y CHAT MANUAL 🔥
    wa_service = WhatsAppService()
    marca = await db.get(ConfiguracionSistema, 1)
    empresa_nombre = getattr(marca, "empresa_nombre", None) or "FdezNet"
    asistente_nombre = getattr(marca, "sistema_nombre", None) or "FdezBot"

    # =========================================================
    # 1. CHAT NORMAL Y GUARDADO EN CRM 
    # =========================================================
    numero_base = telefono_busqueda.split('@')[0]
    ultimos_10 = numero_base[-10:] if len(numero_base) >= 10 else numero_base
    
    stmt_h = select(ClienteModel).where(cast(ClienteModel.telefono, String).like(f"%{ultimos_10}%"))
    cliente_h = (await db.execute(stmt_h)).scalars().first()

    texto_historial = mensaje_texto
    if media_url:
        if "[FOTO_COMPROBANTE]" in mensaje_texto:
            texto_historial = f"📷 [Imagen enviada] {media_url}"
        else:
            texto_historial = f"📎 [Archivo adjunto] {media_url}"

    nuevo_mensaje = MensajeChatModel(
        cliente_id=cliente_h.id if cliente_h else None,
        telefono=telefono_raw, 
        direccion="entrada",
        mensaje=texto_historial,
        leido=False,
        estado_envio="recibido",
    )
    db.add(nuevo_mensaje)
    await db.commit()

    await manager.broadcast({
        "type": "NEW_MESSAGE",
        "data": {
            "id": nuevo_mensaje.id,
            "cliente_id": nuevo_mensaje.cliente_id,
            "mensaje": nuevo_mensaje.mensaje,
            "direccion": "entrada"
        }
    })

    fuera_de_horario = esta_fuera_de_horario()
    if fuera_de_horario and telefono_raw not in bot_memory:
        await wa_service.enviar_mensaje(
            telefono=telefono_raw,
            mensaje=mensaje_fuera_de_horario(empresa_nombre, asistente_nombre),
            tipo_evento="presentacion_bot_fuera_horario",
        )

    # El autoservicio es únicamente por texto; no se envía audio a servicios externos.
    if media_url and "[AUDIO]" in mensaje_texto.upper():
        if fuera_de_horario and telefono_raw not in bot_memory:
            bot_memory[telefono_raw] = {
                "paso": "ESPERANDO_OPCION",
                "iniciado_en": datetime.now(),
            }
        await wa_service.enviar_mensaje(
            telefono=telefono_raw,
            mensaje=(
                mensaje_audio_no_disponible(asistente_nombre)
                + ("\n\n" + construir_menu_bot(asistente_nombre) if fuera_de_horario else "")
            ),
        )
        return {"status": "audio_no_disponible"}

    # =========================================================
    # 2. ACTIVACIÓN DEL BOT (NUEVA PALABRA CLAVE)
    # =========================================================
    if texto_limpio == BOT_KEYWORD:
        bot_memory[telefono_raw] = {
            "paso": "ESPERANDO_OPCION",
            "iniciado_en": datetime.now(),
        }
        menu = construir_menu_bot(asistente_nombre)
        await wa_service.enviar_mensaje(telefono=telefono_raw, mensaje=menu)
        return {"status": "bot_iniciado"}

    if texto_limpio == "fdezpay":
        await wa_service.enviar_mensaje(
            telefono=telefono_raw,
            mensaje="🤖 La palabra de acceso cambió. Escribe *fdezbot* para iniciar.",
        )
        return {"status": "palabra_anterior"}

    # Fuera del horario, cualquier mensaje inicia el autoservicio. Una foto de
    # comprobante entra directamente al análisis, sin exigir una palabra clave.
    if fuera_de_horario and telefono_raw not in bot_memory:
        intencion = detectar_intencion_bot(
            mensaje_texto,
            es_comprobante=bool(
                media_url
                and "[FOTO_COMPROBANTE]" in mensaje_texto.upper()
            ),
        )
        paso_por_intencion = {
            "pago": "ESPERANDO_FOTO_PAGO",
            "promesa": "VALIDAR_CEDULA_PROMESA",
            "estado": "VALIDAR_CEDULA_ESTADO",
            "sin_internet": "VALIDAR_CEDULA_SOPORTE",
            "menu": "ESPERANDO_OPCION",
        }
        if intencion == "datos_pago":
            await wa_service.enviar_mensaje(
                telefono=telefono_raw,
                mensaje=await obtener_datos_pago(db),
            )
            return {"status": "bot_datos_pago"}

        bot_memory[telefono_raw] = {
            "paso": paso_por_intencion[intencion],
            "iniciado_en": datetime.now(),
        }
        if intencion == "promesa":
            await wa_service.enviar_mensaje(
                telefono=telefono_raw,
                mensaje=(
                    "⏳ Puedo ayudarte a registrar una promesa y, si aplica, "
                    "reactivar tu servicio. Escribe tu *número de contrato*."
                ),
            )
            return {"status": "bot_promesa_automatico"}
        if intencion == "estado":
            await wa_service.enviar_mensaje(
                telefono=telefono_raw,
                mensaje=(
                    "📊 Para consultar tu servicio y saldo, escribe tu "
                    "*número de contrato*."
                ),
            )
            return {"status": "bot_estado_automatico"}
        if intencion == "sin_internet":
            await wa_service.enviar_mensaje(
                telefono=telefono_raw,
                mensaje=(
                    "📡 Vamos a revisar tu servicio. Escribe tu "
                    "*número de contrato*."
                ),
            )
            return {"status": "bot_soporte_automatico"}
        if intencion == "menu":
            await wa_service.enviar_mensaje(
                telefono=telefono_raw,
                mensaje=construir_menu_bot(asistente_nombre),
            )
            return {"status": "bot_automatico"}

    # =========================================================
    # 3. LÓGICA DEL BOT 
    # =========================================================
    if telefono_raw in bot_memory:
        estado = bot_memory[telefono_raw]

        iniciado_en = estado.get("iniciado_en")
        if iniciado_en and datetime.now() - iniciado_en > timedelta(
            minutes=BOT_SESSION_MINUTES
        ):
            del bot_memory[telefono_raw]
            await wa_service.enviar_mensaje(
                telefono=telefono_raw,
                mensaje="⌛ La sesión terminó por seguridad. Escribe *fdezbot* para iniciar otra.",
            )
            return {"status": "bot_expirado"}

        if texto_limpio in {"menu", "menú"}:
            estado.clear()
            estado.update({"paso": "ESPERANDO_OPCION", "iniciado_en": datetime.now()})
            await wa_service.enviar_mensaje(
                telefono=telefono_raw,
                mensaje=construir_menu_bot(asistente_nombre),
            )
            return {"status": "bot_menu"}

        if texto_limpio in {"cancelar", "salir"}:
            del bot_memory[telefono_raw]
            await wa_service.enviar_mensaje(telefono=telefono_raw, mensaje="🤖 Asistente desactivado. Un asesor humano te atenderá a la brevedad. ¡Buen día!")
            return {"status": "bot_apagado"}

        # --- SELECCIÓN DEL MENÚ ---
        if estado["paso"] == "ESPERANDO_OPCION":
            if texto_limpio == "1":
                estado["paso"] = "ESPERANDO_FOTO_PAGO"
                res = "📄 *Reporte de Pago*\nPor favor, envíame la **foto del comprobante** o ticket bien enfocada."
                
            elif texto_limpio == "2":
                estado["paso"] = "VALIDAR_CEDULA_PROMESA"
                res = "⏳ *Promesa de Pago*\nPor favor escribe tu *número de contrato* para buscar tu cuenta (Ej: 329B)."
                
            elif texto_limpio == "3":
                estado["paso"] = "VALIDAR_CEDULA_ESTADO"
                res = "📊 *Estado del Servicio*\nPor favor, escribe tu *número de contrato* para buscar tus datos."

            elif texto_limpio == "4":
                res = await obtener_datos_pago(db)
                del bot_memory[telefono_raw]

            elif texto_limpio == "5":
                estado["paso"] = "VALIDAR_CEDULA_SOPORTE"
                res = (
                    "📡 *Revisión de conexión*\nEscribe tu "
                    "*número de contrato* para revisar tu servicio."
                )
                
            else:
                res = "❌ Opción no válida. Responde con un número del 1 al 5. (Escribe 'cancelar' para salir)."
            
            await wa_service.enviar_mensaje(telefono=telefono_raw, mensaje=res)
            return {"status": "procesando_menu"}

        # --- FLUJO 1: REPORTAR PAGO ---
        elif estado["paso"] == "ESPERANDO_FOTO_PAGO":
            if media_url and "[FOTO_COMPROBANTE]" in mensaje_texto:
                await wa_service.enviar_mensaje(telefono=telefono_raw, mensaje="🤖 Analizando tu comprobante... ⏳")
                
                resultado_ocr = await ocr_tool.procesar_ticket(media_url)

                monto_detectado = Decimal(
                    str(resultado_ocr.get("monto") or 0)
                )
                folio_banco = normalize_reference(resultado_ocr.get("folio"))

                if resultado_ocr["exito"] and folio_banco:
                    folio_canonico = normalize_reference_for_match(folio_banco)
                    pago_existente = (
                        await db.execute(
                            select(PagoAutovalidadoModel)
                            .where(
                                referencia_canonica_sql(
                                    PagoAutovalidadoModel.folio_banco
                                ) == folio_canonico
                            )
                            .limit(1)
                        )
                    ).scalars().first()
                    comprobante_existente = (
                        await db.execute(
                            select(ComprobantePagoRevisionModel)
                            .where(
                                ComprobantePagoRevisionModel.folio_detectado.is_not(None),
                                referencia_canonica_sql(
                                    ComprobantePagoRevisionModel.folio_detectado
                                ) == folio_canonico,
                            )
                            .order_by(ComprobantePagoRevisionModel.id.desc())
                            .limit(1)
                        )
                    ).scalars().first()
                    if pago_existente or comprobante_existente:
                        del bot_memory[telefono_raw]
                        await wa_service.enviar_mensaje(
                            telefono=telefono_raw,
                            mensaje=mensaje_comprobante_ya_recibido(
                                comprobante_existente.estado
                                if comprobante_existente
                                else None,
                                pago_registrado=bool(
                                    pago_existente
                                    or (
                                        comprobante_existente
                                        and comprobante_existente.pago_id
                                    )
                                ),
                            ),
                        )
                        return {"status": "comprobante_duplicado"}

                revision = ComprobantePagoRevisionModel(
                    cliente_id=cliente_h.id if cliente_h else None,
                    mensaje_chat_id=nuevo_mensaje.id,
                    telefono=telefono_raw,
                    media_url=media_url,
                    monto_detectado=(
                        monto_detectado if monto_detectado > 0 else None
                    ),
                    folio_detectado=folio_banco,
                    cedula_detectada=resultado_ocr.get("cedula_detectada"),
                    motivo_revision=(
                        "esperando_confirmacion"
                        if resultado_ocr["exito"]
                        else "ocr_no_legible"
                    ),
                )
                db.add(revision)
                await db.commit()
                await db.refresh(revision)
                estado["revision_id"] = revision.id
                
                if not resultado_ocr["exito"]:
                    estado["paso"] = "CONFIRMAR_PROMESA_COMPROBANTE"
                    await wa_service.enviar_mensaje(
                        telefono=telefono_raw,
                        mensaje=(
                            "⚠️ No pude leer con seguridad el folio o monto. "
                            "La imagen quedó guardada para revisión humana y "
                            "*no se registró un pago automático*.\n\n"
                            "Si tu servicio está suspendido, puedo iniciar una "
                            "promesa de pago para intentar reactivarlo. "
                            "¿Deseas continuar? Responde *SI* o *NO*."
                        ),
                    )
                    return {"status": "ocr_failed"}

                # 🛡️ CAPA 1: Evitar Folios Duplicados
                folio_banco = normalize_reference(resultado_ocr["folio"])
                stmt_fraude = select(PagoAutovalidadoModel).where(PagoAutovalidadoModel.folio_banco == folio_banco)
                pago_existente = (await db.execute(stmt_fraude)).scalars().first()

                if pago_existente:
                    revision.estado = "rechazado"
                    revision.motivo_revision = "folio_duplicado"
                    revision.fecha_revision = datetime.now()
                    await db.commit()
                    res_fraude = f"🚫 *¡Alerta de Seguridad!*\n\nEl comprobante con folio *{folio_banco}* ya fue registrado anteriormente. Este intento ha sido bloqueado. Si es un error, contacta a soporte."
                    del bot_memory[telefono_raw]
                    await wa_service.enviar_mensaje(telefono=telefono_raw, mensaje=res_fraude)
                    
                    # 🚨 ALERTA AL ADMINISTRADOR 🚨
                    config_res = await db.execute(select(ConfiguracionSistema).where(ConfiguracionSistema.id == 1))
                    config = config_res.scalar_one_or_none()
                    if config and config.telefonos_alerta:
                        numeros_admin = [n.strip() for n in config.telefonos_alerta.split(",") if n.strip()]
                        alerta_admin = f"🚨 *INTENTO DE FRAUDE DETECTADO*\nEl número {telefono_raw} intentó subir un ticket duplicado con folio {folio_banco}."
                        for admin_num in numeros_admin:
                            await wa_service.enviar_mensaje(telefono=admin_num, mensaje=alerta_admin)
                            
                    return {"status": "fraude_detectado"}

                # Guardamos los datos leídos en memoria
                estado["pago"] = resultado_ocr
                cedula_ocr = resultado_ocr.get("cedula_detectada")

                # 🛡️ CAPA 2: Triangulación de Identidad
                if cedula_ocr:
                    stmt_c_ocr = select(ClienteModel).where(ClienteModel.cedula == cedula_ocr)
                    cliente_ocr = (await db.execute(stmt_c_ocr)).scalars().first()
                    
                    if cliente_ocr:
                        revision.cliente_id = cliente_ocr.id
                        await db.commit()
                        estado["paso"] = "VALIDACION_FINAL_PAGO" 
                        estado["cliente_id"] = cliente_ocr.id
                        res = f"Detecté el número de contrato *{cedula_ocr}* en el comprobante.\n\n¿Deseas aplicar el pago de *${resultado_ocr['monto']}* a la cuenta de *{cliente_ocr.nombre}*? (Responde *SI* o *NO*)"
                        await wa_service.enviar_mensaje(telefono=telefono_raw, mensaje=res)
                        return {"status": "confirmar_ocr"}

                if cliente_h:
                    revision.cliente_id = cliente_h.id
                    await db.commit()
                    estado["paso"] = "CONFIRMAR_NOMBRE_PAGO"
                    estado["cliente_id"] = cliente_h.id
                    res = f"Detecto que envías desde el celular de *{cliente_h.nombre}*.\n\nHe leído un pago por *${resultado_ocr['monto']}*.\n¿Deseas aplicar este pago a tu cuenta? (Responde *SI* o *NO*)"
                else:
                    estado["paso"] = "PEDIR_CEDULA_PAGO"
                    res = f"He leído tu comprobante por *${resultado_ocr['monto']}*.\n¿A qué cuenta aplicamos el pago? Escribe el *número de contrato* (Ej. 329B)."

                await wa_service.enviar_mensaje(telefono=telefono_raw, mensaje=res)
                return {"status": "bot_init_pago"}
            else:
                await wa_service.enviar_mensaje(telefono=telefono_raw, mensaje="⚠️ Estoy esperando una FOTO de tu comprobante. (Escribe 'cancelar' para salir).")
                return {"status": "esperando_foto"}

        elif estado["paso"] == "CONFIRMAR_PROMESA_COMPROBANTE":
            if texto_limpio in {"si", "sí", "s"}:
                estado["paso"] = "VALIDAR_CEDULA_PROMESA"
                res = (
                    "⏳ Escribe tu *número de contrato* para registrar la "
                    "promesa y revisar si se puede reactivar el servicio."
                )
            elif texto_limpio in {"no", "n"}:
                del bot_memory[telefono_raw]
                res = (
                    "✅ Entendido. Tu comprobante quedó en el chat para que "
                    "un asesor lo verifique en el próximo horario de atención."
                )
            else:
                res = "Por favor responde *SI* o *NO*."
            await wa_service.enviar_mensaje(telefono=telefono_raw, mensaje=res)
            return {"status": "promesa_comprobante"}

        elif estado["paso"] == "CONFIRMAR_NOMBRE_PAGO":
            if "si" in texto_limpio:
                estado["paso"] = "VALIDACION_FINAL_PAGO" 
                res = "¡Perfecto! ✅ Escribe tu *número de contrato* como firma de seguridad para confirmar la transacción."
            else:
                estado["paso"] = "PEDIR_CEDULA_PAGO"
                res = "Entendido. Escribe el *número de contrato* al que deseas aplicar el pago (Ej. la cuenta de un familiar)."
            
            await wa_service.enviar_mensaje(telefono=telefono_raw, mensaje=res)
            return {"status": "bot_confirmando_pago"}

        elif estado["paso"] == "PEDIR_CEDULA_PAGO":
            cedula_input = mensaje_texto.upper().strip()
            stmt_c = select(ClienteModel).where(ClienteModel.cedula == cedula_input)
            cliente_final = (await db.execute(stmt_c)).scalars().first()
            
            if cliente_final:
                revision_id = estado.get("revision_id")
                revision = (
                    await db.get(ComprobantePagoRevisionModel, revision_id)
                    if revision_id
                    else None
                )
                if revision:
                    revision.cliente_id = cliente_final.id
                    await db.commit()
                estado["paso"] = "VALIDACION_FINAL_PAGO"
                estado["cliente_id"] = cliente_final.id
                return await procesar_validacion_final_pago("si", estado, telefono_raw, db, wa_service)
            else:
                res = "❌ Número de contrato incorrecto. Inténtalo de nuevo o escribe 'cancelar'."
                await wa_service.enviar_mensaje(telefono=telefono_raw, mensaje=res)
                return {"status": "cedula_invalida"}

        elif estado["paso"] == "VALIDACION_FINAL_PAGO":
            return await procesar_validacion_final_pago(mensaje_texto, estado, telefono_raw, db, wa_service)

        # --- FLUJO 2: PROMESA DE PAGO ---
        elif estado["paso"] == "VALIDAR_CEDULA_PROMESA":
            cedula_input = mensaje_texto.upper().strip()
            stmt_c = select(ClienteModel).where(ClienteModel.cedula == cedula_input)
            cliente_final = (await db.execute(stmt_c)).scalars().first()

            if cliente_final:
                factura = await obtener_factura_cobrable(
                    db,
                    cliente_final.id,
                )

                if not factura:
                    res = "✅ No tienes facturas pendientes, tu servicio está al corriente."
                    del bot_memory[telefono_raw]
                elif factura.es_promesa_activa:
                    res = f"⚠️ Ya tienes una promesa de pago activa hasta el {factura.fecha_promesa_pago}. No es posible agregar otra."
                    del bot_memory[telefono_raw]
                else:
                    estado["paso"] = "PEDIR_DIA_PROMESA"
                    estado["cliente_id"] = cliente_final.id
                    estado["factura_id"] = factura.id
                    res = (
                        f"Hola, *{cliente_final.nombre}*.\n\n"
                        f"Tu saldo pendiente es *${Decimal(factura.saldo_pendiente or 0):.2f}*.\n"
                        "¿Qué día realizarás el pago?\n"
                        "👉 Escribe el día (ejemplo: *15*) o la fecha completa "
                        "(ejemplo: *15/09/2026*)."
                    )
            else:
                res = "❌ Número de contrato incorrecto. Inténtalo de nuevo o escribe 'cancelar'."
            
            await wa_service.enviar_mensaje(telefono=telefono_raw, mensaje=res)
            return {"status": "pidiendo_dia_promesa"}

        elif estado["paso"] == "PEDIR_DIA_PROMESA":
            try:
                fecha_promesa = interpretar_fecha_promesa(texto_limpio)

                cliente_final = await db.get(ClienteModel, estado["cliente_id"])
                factura = await db.get(FacturaModel, estado["factura_id"])

                (
                    promesa,
                    factura,
                    cliente_final,
                    politica,
                    reactivado,
                ) = (
                    await BillingService(db).registrar_promesa_y_reactivar(
                        factura.id,
                        fecha_promesa,
                        usuario_id=None,
                        notas="Registrada por autoservicio de WhatsApp",
                        enviar_notificaciones=False,
                        origen="bot",
                    )
                )

                mensaje_reconexion = (
                    "\nTu servicio ha sido reactivado. 🚀"
                    if reactivado
                    else ""
                )
                res = (
                    f"✅ ¡Promesa registrada, {cliente_final.nombre}!\n\n"
                    f"Tienes hasta el "
                    f"*{fecha_promesa.strftime('%d/%m/%Y')}* "
                    f"para realizar tu pago. El corte se aplicará al día "
                    f"siguiente si continúa pendiente.{mensaje_reconexion}"
                )
                del bot_memory[telefono_raw]

            except ValueError as exc:
                await db.rollback()
                res = f"❌ No fue posible registrar la promesa: {exc}"

            await wa_service.enviar_mensaje(telefono=telefono_raw, mensaje=res)
            return {"status": "promesa_finalizada"}

        # --- FLUJO 4: DIAGNÓSTICO BÁSICO SIN INTERNET ---
        elif estado["paso"] == "VALIDAR_CEDULA_SOPORTE":
            cedula_input = mensaje_texto.upper().strip()
            cliente_final = (
                await db.execute(
                    select(ClienteModel).where(
                        ClienteModel.cedula == cedula_input
                    )
                )
            ).scalars().first()
            if not cliente_final:
                res = (
                    "❌ Número de contrato incorrecto. Inténtalo nuevamente o escribe "
                    "'cancelar'."
                )
            elif cliente_final.estado == "suspendido":
                factura = await obtener_factura_cobrable(db, cliente_final.id)
                if not factura:
                    del bot_memory[telefono_raw]
                    res = (
                        f"🔴 Hola, *{cliente_final.nombre}*. Tu servicio aparece "
                        "suspendido, pero no encontré una deuda a la cual aplicar "
                        "una promesa. El caso quedó registrado para revisión."
                    )
                elif factura.es_promesa_activa:
                    del bot_memory[telefono_raw]
                    res = (
                        "⚠️ Ya tienes una promesa activa hasta el "
                        f"{factura.fecha_promesa_pago}. El reporte quedó "
                        "registrado para que un asesor revise la conexión."
                    )
                else:
                    estado["paso"] = "PEDIR_DIA_PROMESA"
                    estado["cliente_id"] = cliente_final.id
                    estado["factura_id"] = factura.id
                    res = (
                        f"🔴 Hola, *{cliente_final.nombre}*. Tu servicio aparece "
                        "suspendido y tienes un saldo de "
                        f"*${Decimal(factura.saldo_pendiente or 0):.2f}*.\n\n"
                        "Puedo registrar una promesa e intentar reactivarlo. "
                        "Escribe el día en que pagarás (ejemplo: *15*) o la "
                        "fecha completa (ejemplo: *15/09/2026*)."
                    )
            else:
                del bot_memory[telefono_raw]
                res = (
                    f"🟢 Hola, *{cliente_final.nombre}*. Tu cuenta no aparece "
                    "suspendida. Desconecta la ONU y el router de la corriente "
                    "durante 30 segundos y vuelve a conectarlos.\n\n"
                    "Si la luz *LOS* está roja o sigues sin internet después de "
                    "5 minutos, deja ambos equipos encendidos. Tu reporte quedó "
                    "registrado para revisión de un asesor."
                )
            await wa_service.enviar_mensaje(telefono=telefono_raw, mensaje=res)
            return {"status": "soporte_basico"}

        # --- FLUJO 3: ESTADO DEL SERVICIO ---
        elif estado["paso"] == "VALIDAR_CEDULA_ESTADO":
            cedula_input = mensaje_texto.upper().strip()
            stmt_c = select(ClienteModel).options(joinedload(ClienteModel.plan)).where(ClienteModel.cedula == cedula_input)
            cliente_final = (await db.execute(stmt_c)).scalars().first()

            if cliente_final:
                nombre_plan = cliente_final.plan.nombre if cliente_final.plan else "Sin plan"
                megas_bajada = (cliente_final.plan.velocidad_bajada / 1024) if cliente_final.plan else 0
                etiquetas_estado = {
                    "activo": "🟢 ACTIVO",
                    "suspendido": "🔴 SUSPENDIDO",
                    "pendiente_instalacion": "🟡 PENDIENTE DE INSTALACIÓN",
                    "cancelado": "⚫ CANCELADO",
                    "retirado": "⚫ RETIRADO",
                }
                estado_str = etiquetas_estado.get(
                    cliente_final.estado,
                    f"🟠 {cliente_final.estado.upper()}",
                )

                facturas_abiertas = (
                    await db.execute(
                        select(FacturaModel)
                        .where(
                            FacturaModel.cliente_id == cliente_final.id,
                            FacturaModel.estado.in_(
                                ["pendiente", "vencida"]
                            ),
                            FacturaModel.saldo_pendiente > 0,
                        )
                        .order_by(FacturaModel.fecha_vencimiento.asc())
                    )
                ).scalars().all()
                deuda_total = sum(
                    (
                        Decimal(factura.saldo_pendiente or 0)
                        for factura in facturas_abiertas
                    ),
                    Decimal("0.00"),
                )

                siguiente = (
                    facturas_abiertas[0]
                    if facturas_abiertas
                    else None
                )
                if (
                    siguiente
                    and siguiente.es_promesa_activa
                    and siguiente.fecha_promesa_pago
                ):
                    fecha_financiera = (
                        "Promesa vigente hasta "
                        f"{siguiente.fecha_promesa_pago.strftime('%d/%m/%Y')}"
                    )
                elif siguiente and siguiente.fecha_vencimiento:
                    fecha_financiera = (
                        "Vencimiento más próximo: "
                        f"{siguiente.fecha_vencimiento.strftime('%d/%m/%Y')}"
                    )
                else:
                    fecha_financiera = "Sin pagos pendientes"
                
                res = (
                    f"📊 *ESTADO DE TU SERVICIO*\n\n"
                    f"👤 *Titular:* {cliente_final.nombre}\n"
                    f"📡 *Plan actual:* {nombre_plan} ({int(megas_bajada)} Mbps)\n"
                    f"🌐 *IP:* {cliente_final.ip_asignada or 'Dinámica'}\n"
                    f"🔌 *Estado:* {estado_str}\n"
                    f"💳 *Saldo pendiente:* ${deuda_total:.2f}\n"
                    f"📅 *Cobranza:* {fecha_financiera}\n"
                    f"💰 *Saldo a favor:* ${Decimal(cliente_final.saldo_a_favor or 0):.2f}\n\n"
                    f"Para volver al menú escribe *fdezbot*."
                )
                del bot_memory[telefono_raw]
            else:
                res = "❌ Número de contrato incorrecto. Inténtalo de nuevo o escribe 'cancelar'."
            
            await wa_service.enviar_mensaje(telefono=telefono_raw, mensaje=res)
            return {"status": "bot_estado_finished"}

    if esta_fuera_de_horario():
        await wa_service.enviar_mensaje(
            telefono=telefono_raw,
            mensaje=mensaje_fuera_de_horario(empresa_nombre, asistente_nombre),
            tipo_evento="respuesta_fuera_horario",
        )
        return {"status": "fuera_de_horario"}

    return {"status": "chat_normal"}

@webhook_router.post("/webhook/ack")
async def webhook_actualizar_ack(
    data: AckWebhookRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_webhook_secret),
):
    mensaje = await WhatsAppOutboxService(db).actualizar_ack(
        ack=data.ack,
        wa_id=data.wa_id,
        mensaje_chat_id=data.mensaje_chat_id,
    )
    if mensaje:
        await manager.broadcast({
            "type": "MESSAGE_ACK",
            "data": {
                "id": mensaje.id,
                "wa_id": mensaje.wa_id,
                "ack": mensaje.ack,
                "estado_envio": mensaje.estado_envio,
                "cliente_id": mensaje.cliente_id,
            },
        })
    return {"status": "ok", "matched": mensaje is not None}


# ==========================================
# 📤 BANDEJA OPERATIVA DE SALIDA
# ==========================================
def serializar_salida(mensaje: MensajeChatModel):
    return {
        "id": mensaje.id,
        "cliente_id": mensaje.cliente_id,
        "cliente": (
            {
                "id": mensaje.cliente.id,
                "nombre": mensaje.cliente.nombre,
            }
            if mensaje.cliente
            else None
        ),
        "telefono": mensaje.telefono,
        "mensaje": mensaje.mensaje,
        "tipo_mensaje": mensaje.tipo_mensaje,
        "tipo_evento": mensaje.tipo_evento,
        "lote_id": mensaje.lote_id,
        "intervalo_salida": mensaje.intervalo_salida,
        "estado_envio": mensaje.estado_envio,
        "ack": mensaje.ack,
        "wa_id": mensaje.wa_id,
        "intentos": mensaje.intentos,
        "max_intentos": mensaje.max_intentos,
        "reintentos_manuales": mensaje.reintentos_manuales,
        "ultimo_error": mensaje.ultimo_error,
        "fecha": mensaje.fecha,
        "ultima_tentativa_en": mensaje.ultima_tentativa_en,
        "proximo_intento_en": mensaje.proximo_intento_en,
        "enviado_en": mensaje.enviado_en,
        "entregado_en": mensaje.entregado_en,
        "leido_en": mensaje.leido_en,
        "ruta_archivo": mensaje.ruta_archivo,
        "creado_por": (
            {
                "id": mensaje.creado_por.id,
                "nombre": mensaje.creado_por.nombre_completo,
            }
            if mensaje.creado_por
            else None
        ),
        "ultimo_reintento_por": (
            {
                "id": mensaje.ultimo_reintento_por.id,
                "nombre": mensaje.ultimo_reintento_por.nombre_completo,
            }
            if mensaje.ultimo_reintento_por
            else None
        ),
    }


@router.get("/salidas")
async def listar_salidas_whatsapp(
    estado: Optional[str] = None,
    tipo_evento: Optional[str] = None,
    cliente_id: Optional[int] = None,
    busqueda: Optional[str] = None,
    desde: Optional[date] = None,
    hasta: Optional[date] = None,
    lote_id: Optional[str] = None,
    pagina: int = 1,
    limite: int = 50,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    if pagina < 1 or limite < 1 or limite > 200:
        raise HTTPException(400, "Paginación inválida")
    if estado and estado not in ESTADOS_SALIDA:
        raise HTTPException(400, "Estado de envío inválido")
    if desde and hasta and hasta < desde:
        raise HTTPException(400, "La fecha final no puede ser anterior")
    try:
        items, total, resumen = await WhatsAppOutboxService(db).listar(
            estado=estado,
            tipo_evento=tipo_evento,
            cliente_id=cliente_id,
            busqueda=busqueda,
            desde=desde,
            hasta=hasta,
            lote_id=lote_id,
            pagina=pagina,
            limite=limite,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "items": [serializar_salida(item) for item in items],
        "total": total,
        "pagina": pagina,
        "limite": limite,
        "resumen": resumen,
        "cola_memoria": whatsapp_queue.queue.qsize(),
    }


@router.post("/salidas/reintentar-fallidos")
async def reintentar_salidas_fallidas(
    datos: ReintentoMasivoRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    ids = await WhatsAppOutboxService(db).reintentar_lote(
        current_user,
        ids=datos.ids,
        limite=datos.limite,
    )
    return {
        "status": "encolados",
        "total": len(ids),
        "ids": ids,
    }


@router.get("/salidas/{mensaje_id}")
async def obtener_salida_whatsapp(
    mensaje_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    try:
        registro = await WhatsAppOutboxService(db).obtener(mensaje_id)
        return serializar_salida(registro)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/salidas/{mensaje_id}/reintentar")
async def reintentar_salida_whatsapp(
    mensaje_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    try:
        registro = await WhatsAppOutboxService(db).reintentar(
            mensaje_id,
            current_user,
        )
        return serializar_salida(registro)
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(409, str(exc)) from exc


@router.delete("/salidas/{mensaje_id}")
async def eliminar_salida_whatsapp(
    mensaje_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    try:
        await WhatsAppOutboxService(db).eliminar(mensaje_id)
        return {"status": "eliminado", "id": mensaje_id}
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(409, str(exc)) from exc


# ==========================================
# 💬 ENDPOINTS DEL CHAT CRM (REACT)
# ==========================================
@router.get("/no-leidos")
async def obtener_no_leidos(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    stmt = select(
        MensajeChatModel.cliente_id, 
        func.count(MensajeChatModel.id),
        func.min(MensajeChatModel.fecha) 
    ).where(
        MensajeChatModel.direccion == 'entrada',
        MensajeChatModel.leido == False,
        MensajeChatModel.cliente_id.isnot(None)
    ).group_by(MensajeChatModel.cliente_id)
    
    filas = (await db.execute(stmt)).all()
    return {
        str(fila[0]): {"count": fila[1], "antiguedad": fila[2].isoformat() if fila[2] else None} 
        for fila in filas
    }

@router.get("/chat/{cliente_id}")
async def obtener_historial_chat(
    cliente_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(
        role_required(["admin", "supervisor", "tecnico"])
    ),
):
    try:
        await verificar_acceso_cliente(db, current_user, cliente_id)
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    stmt = select(MensajeChatModel).where(MensajeChatModel.cliente_id == cliente_id).order_by(MensajeChatModel.fecha.asc())
    mensajes = (await db.execute(stmt)).scalars().all()

    mensajes_no_leidos = [m for m in mensajes if m.direccion == 'entrada' and not m.leido]
    if mensajes_no_leidos:
        for m in mensajes_no_leidos: m.leido = True
        await db.commit()
    return mensajes

@router.post("/chat/{cliente_id}/enviar")
async def enviar_mensaje_chat(
    cliente_id: int,
    data: MensajeEnviarRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(
        role_required(["admin", "supervisor", "tecnico"])
    ),
):
    try:
        await verificar_acceso_cliente(db, current_user, cliente_id)
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    cliente = await db.get(ClienteModel, cliente_id)
    if not cliente or not cliente.telefono: raise HTTPException(status_code=404, detail="Cliente no encontrado")

    telefono_limpio = cliente.telefono.replace("+", "").replace(" ", "")
    if len(telefono_limpio) == 10: telefono_limpio = f"521{telefono_limpio}"
    elif len(telefono_limpio) == 12 and telefono_limpio.startswith("52"): telefono_limpio = f"521{telefono_limpio[2:]}"

    nuevo_mensaje = MensajeChatModel(
        cliente_id=cliente.id,
        telefono=telefono_limpio,
        direccion="salida",
        mensaje=data.mensaje,
        tipo_mensaje="texto",
        tipo_evento="chat_manual",
        leido=True,
        ack=0,
        estado_envio="pendiente",
        creado_por_id=current_user.id,
    )
    db.add(nuevo_mensaje)
    await db.commit()
    await db.refresh(nuevo_mensaje)

    await whatsapp_queue.agregar_tarea(
        {
            "mensaje_chat_id": nuevo_mensaje.id,
            "intervalo": 0,
        }
    )
    return {
        "status": "encolado",
        "mensaje_id": nuevo_mensaje.id,
        "estado_envio": nuevo_mensaje.estado_envio,
    }

@webhook_router.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: str):
    token = websocket.query_params.get("token", "")
    try:
        username = decode_access_token(token)
        async with SessionLocal() as db:
            result = await db.execute(
                select(UsuarioModel).where(UsuarioModel.usuario == username)
            )
            user = result.scalar_one_or_none()
            if (
                not user
                or not user.activo
                or user.rol not in {"admin", "supervisor"}
                or str(user.id) != str(user_id)
            ):
                raise ValueError("Usuario no autorizado")
    except Exception:
        await websocket.close(code=1008, reason="No autorizado")
        return

    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except Exception:
        manager.disconnect(websocket)


# ==========================================
# ⚙️ CONTROL DEL MOTOR NODE.JS
# ==========================================
@router.get("/status")
async def obtener_estado(
    current_user=Depends(role_required(["admin"])),
):
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{BASE_NODE_URL}/status",
                headers=NODE_HEADERS,
                timeout=5.0,
            )
            return resp.json() 
    except Exception as e:
        return {"active": False, "connected": False, "qr": None}

@router.post("/init")
async def iniciar_whatsapp(
    current_user=Depends(role_required(["admin"])),
):
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{BASE_NODE_URL}/init",
                headers=NODE_HEADERS,
                timeout=15.0,
            )
            return resp.json()
    except Exception as e:
        raise HTTPException(status_code=500, detail="No se pudo arrancar el motor de WhatsApp")

@router.post("/logout")
async def logout_whatsapp(
    current_user=Depends(role_required(["admin"])),
):
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{BASE_NODE_URL}/logout",
                headers=NODE_HEADERS,
                timeout=10.0,
            )
            return resp.json()
    except Exception as e:
        raise HTTPException(status_code=500, detail="Error de comunicación con el motor")
