"""Comprobantes de pago recibidos por WhatsApp.

Misma lógica que el flujo "Reportar pago" del bot de menú, separada de los
mensajes para que el agente de IA la use: la captura se lee con OCR, se
bloquean folios repetidos y el pago SOLO se aplica si el correo bancario
confirma que el dinero llegó a la cuenta del ISP con la misma referencia,
monto y fecha que la captura (BankEmailService.transaction_match_reason).
"""

import logging
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.services.bank_email_service import (
    BankEmailError,
    BankEmailService,
    normalize_reference,
    normalize_reference_for_match,
)
from src.application.services.billing_service import BillingService
from src.application.services.finance_service import FinanceService
from src.application.services.ocr_service import OCRService
from src.infrastructure.models import (
    ClienteModel,
    ComprobantePagoRevisionModel,
    ConfiguracionSistema,
    FacturaModel,
    PagoAutovalidadoModel,
)

logger = logging.getLogger(__name__)
_ocr = OCRService()


def referencia_canonica_sql(columna):
    return func.replace(func.replace(func.upper(columna), "O", "0"), "I", "1")


class ComprobanteService:
    def __init__(self, db: AsyncSession, ocr=None, correo=None, avisar_admin=None):
        self.db = db
        self.ocr = ocr or _ocr
        self.correo = correo or BankEmailService()
        # avisar_admin(numero, texto): se inyecta para no depender de WhatsApp.
        self.avisar_admin = avisar_admin

    async def leer(
        self,
        media_url: str,
        telefono: str,
        cliente_id: int | None,
        mensaje_chat_id: int | None,
    ) -> dict:
        """Lee la captura y la deja registrada para conciliación o revisión."""
        resultado = await self.ocr.procesar_ticket(media_url)
        monto = Decimal(str(resultado.get("monto") or 0))
        folio = normalize_reference(resultado.get("folio"))
        huella = resultado.get("huella")

        if resultado.get("exito") and folio:
            estado_previo = await self._estado_folio_existente(folio)
            if estado_previo:
                return {"estado": "duplicado", "folio": folio, **estado_previo}
        if huella:
            estado_previo = await self._estado_huella_existente(huella, telefono, media_url)
            if estado_previo:
                return {"estado": "duplicado", "folio": folio or huella, **estado_previo}

        revision = ComprobantePagoRevisionModel(
            cliente_id=cliente_id,
            mensaje_chat_id=mensaje_chat_id,
            telefono=telefono,
            media_url=media_url,
            monto_detectado=monto if monto > 0 else None,
            folio_detectado=folio,
            cedula_detectada=resultado.get("cedula_detectada"),
            fecha_pago_detectada=resultado.get("fecha_pago"),
            huella_captura=huella,
            concepto_detectado=(resultado.get("concepto") or "")[:120] or None,
            cuentas_detectadas=",".join(resultado.get("cuentas") or [])[:60] or None,
            motivo_revision=(
                "esperando_confirmacion"
                if resultado.get("exito")
                else "sin_referencia" if monto > 0 else "ocr_no_legible"
            ),
        )
        self.db.add(revision)
        await self.db.commit()
        await self.db.refresh(revision)

        if not resultado.get("exito") and monto > 0:
            # Pantallas de "transferencia exitosa" que no muestran referencia:
            # se puede empatar por monto y hora con el correo del banco.
            return {
                "estado": "sin_referencia",
                "revision_id": revision.id,
                "monto": str(monto),
                "hora_en_captura": resultado.get("fecha_pago"),
                "detalle": (
                    "Se leyó el monto pero la captura no muestra clave de rastreo ni referencia. "
                    "Intenta aplicar_comprobante (se empata por monto y hora); si no se confirma, "
                    "pide el comprobante completo (botón Compartir o Ver detalle) con la clave de rastreo."
                ),
            }
        if not resultado.get("exito"):
            return {
                "estado": "ilegible",
                "revision_id": revision.id,
                "detalle": "No se pudo leer el monto; pide una foto más clara o el comprobante completo.",
            }

        contrato = resultado.get("cedula_detectada")
        cliente_en_captura = None
        if contrato:
            cliente_en_captura = (
                await self.db.execute(select(ClienteModel).where(ClienteModel.cedula == contrato))
            ).scalars().first()
        return {
            "estado": "leido",
            "revision_id": revision.id,
            "monto": str(monto),
            "folio": folio,
            "contrato_en_captura": contrato,
            "cliente_en_captura": cliente_en_captura.nombre if cliente_en_captura else None,
        }

    async def conciliar(self, revision_id: int, cliente_id: int) -> dict:
        """Aplica el pago solo si el correo bancario coincide con la captura."""
        revision = await self.db.get(ComprobantePagoRevisionModel, revision_id)
        if not revision:
            return {"aplicado": False, "estado": "comprobante_no_encontrado"}
        cliente = await self.db.get(ClienteModel, cliente_id)
        factura = await self._factura_cobrable(cliente_id)
        if not factura:
            return {"aplicado": False, "estado": "sin_deuda_pendiente"}

        monto = FinanceService.dinero(revision.monto_detectado or 0)
        deuda = Decimal(factura.saldo_pendiente or 0)
        if monto < deuda:
            revision.motivo_revision = "monto_no_coincide"
            revision.notas_revision = f"OCR ${monto}; deuda ${deuda}"
            await self.db.commit()
            return {"aplicado": False, "estado": "monto_menor_a_la_deuda", "monto": str(monto), "deuda": str(deuda)}

        try:
            revision.cliente_id = cliente_id
            revision.factura_id = factura.id
            await self.db.commit()
            try:
                await self.correo.sync(self.db)
            except BankEmailError as exc:
                revision = await self.db.get(ComprobantePagoRevisionModel, revision_id)
                revision.motivo_revision = "error_sincronizacion_correo"
                revision.notas_revision = str(exc)[:1000]
                await self.db.commit()
            revision = await self.db.get(ComprobantePagoRevisionModel, revision_id)
            resultado = await self.correo.reconcile_revision(self.db, revision)
        except Exception as exc:
            logger.exception("Error conciliando comprobante %s", revision_id)
            await self.db.rollback()
            revision = await self.db.get(ComprobantePagoRevisionModel, revision_id)
            if revision:
                revision.estado = "pendiente"
                revision.motivo_revision = "error_conciliacion_correo"
                revision.notas_revision = str(exc)[:1000]
                await self.db.commit()
            return {"aplicado": False, "estado": "error_conciliacion"}

        if resultado.get("approved"):
            return {
                "aplicado": True,
                "estado": "pago_confirmado_por_banco",
                "monto": str(monto),
                "reactivado": bool(resultado.get("reactivado")),
                "sigue_suspendido": cliente.estado == "suspendido" and not resultado.get("reactivado"),
            }
        if resultado.get("status") == "pago_adelantado":
            return {
                "aplicado": False,
                "estado": "pago_adelantado",
                "monto": str(monto),
                "detalle": "El banco confirmó el pago; es del mes siguiente y se aplica solo ese día.",
                "aplicar_el": resultado.get("aplicar_el"),
            }
        return {"aplicado": False, "estado": resultado.get("status") or "sin_confirmacion_bancaria"}

    async def _estado_folio_existente(self, folio: str) -> dict | None:
        canonico = normalize_reference_for_match(folio)
        pago = (
            await self.db.execute(
                select(PagoAutovalidadoModel)
                .where(referencia_canonica_sql(PagoAutovalidadoModel.folio_banco) == canonico)
                .limit(1)
            )
        ).scalars().first()
        revision = (
            await self.db.execute(
                select(ComprobantePagoRevisionModel)
                .where(
                    ComprobantePagoRevisionModel.folio_detectado.is_not(None),
                    referencia_canonica_sql(ComprobantePagoRevisionModel.folio_detectado) == canonico,
                )
                .order_by(ComprobantePagoRevisionModel.id.desc())
                .limit(1)
            )
        ).scalars().first()
        if not pago and not revision:
            return None
        return {
            "pago_ya_registrado": bool(pago or (revision and revision.pago_id)),
            "estado_revision": revision.estado if revision else None,
        }

    async def _estado_huella_existente(self, huella: str, telefono: str, media_url: str) -> dict | None:
        """La misma captura (monto, hora y cuentas) ya se había recibido."""
        revision = (
            await self.db.execute(
                select(ComprobantePagoRevisionModel)
                .where(ComprobantePagoRevisionModel.huella_captura == huella)
                .order_by(ComprobantePagoRevisionModel.id.desc())
                .limit(1)
            )
        ).scalars().first()
        # Una captura rechazada sin pago se puede volver a revisar.
        if not revision or (revision.estado == "rechazado" and not revision.pago_id):
            return None
        return {
            "pago_ya_registrado": bool(revision.pago_id),
            "estado_revision": revision.estado,
            "revision_id": revision.id,
            # Releer la misma imagen del mismo mensaje no es un reenvío.
            "misma_imagen": revision.media_url == media_url,
            "otro_telefono": revision.telefono != telefono,
        }

    async def alertar_folio_duplicado(self, telefono: str, folio: str) -> None:
        """Mismo aviso de posible fraude que manda el bot de menú."""
        if not self.avisar_admin:
            return
        config = await self.db.get(ConfiguracionSistema, 1)
        numeros = [n.strip() for n in (getattr(config, "telefonos_alerta", "") or "").split(",") if n.strip()]
        texto = (
            "🚨 *POSIBLE FRAUDE*\n"
            f"El número {telefono} envió un comprobante con el folio {folio}, que ya se había registrado."
            + (" (folio generado de la captura: mismo monto, hora y cuentas)" if folio.startswith("SC-") else "")
        )
        for numero in numeros:
            await self.avisar_admin(numero, texto)

    async def _factura_cobrable(self, cliente_id: int):
        factura = (
            await self.db.execute(
                select(FacturaModel)
                .where(
                    FacturaModel.cliente_id == cliente_id,
                    FacturaModel.estado.in_(["pendiente", "vencida"]),
                    FacturaModel.saldo_pendiente > 0,
                )
                .order_by(FacturaModel.fecha_vencimiento.asc())
            )
        ).scalars().first()
        if not factura:
            return None
        try:
            cobrable, _, _ = await BillingService(self.db).preparar_factura_cobrable(
                factura.id, fecha_reactivacion=datetime.now().date()
            )
        except ValueError:
            await self.db.commit()
            return None
        await self.db.commit()
        return cobrable
