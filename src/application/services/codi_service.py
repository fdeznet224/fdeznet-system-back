"""Cobro CoDi con pey.mx.

El agente de WhatsApp manda un cobro a la app del banco del cliente (CoDi por
número de celular) por lo que debe. Cuando el cliente lo acepta, pey avisa por
webhook y el pago se aplica solo, igual que uno de la Terminal de Cobro: se
reparte de la factura más vieja a la más nueva, reactiva el servicio y le
llega el recibo por WhatsApp.

Documentación: https://docs.pey.mx (los webhooks se firman con Standard Webhooks).
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.services.pagos_captura import config_pagos
from src.application.services.usuario_sistema_service import usuario_de_sistema
from src.infrastructure.models import (
    ClienteModel,
    CobroCodiModel,
    ConfiguracionCorreoBancoModel,
    ConfiguracionSistema,
    MensajeChatModel,
)

logger = logging.getLogger(__name__)

PEY_API_URL = os.getenv("PEY_API_URL", "https://api.pey.mx").rstrip("/")
# Lo que dura el cobro en la app del banco (CoDi pide al menos 360 s).
VIGENCIA_SEGUNDOS = 900
# Si se pide otra vez enseguida, no se le manda un segundo cobro.
ESPERA_REENVIO = timedelta(minutes=2)
# Tolerancia de Standard Webhooks para la hora del aviso.
TOLERANCIA_FIRMA_SEGUNDOS = 300
USUARIO_CODI = "CoDi"
EVENTOS_PAGO = {"codi.pn.payment_received", "codi.qr.payment_received"}


class FirmaInvalida(Exception):
    """El aviso no viene firmado por pey (o la firma ya caducó)."""


def url_webhook() -> str | None:
    base = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
    return f"{base}/api/pagos/codi/webhook" if base else None


async def codi_disponible(db: AsyncSession) -> bool:
    config = await db.get(ConfiguracionCorreoBancoModel, 1)
    return bool(config and config.codi_activo and (config.pey_api_key or "").strip())


def _monto(valor) -> Decimal:
    try:
        return Decimal(str(valor)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0.00")


def _telefono_10(valor: str | None) -> str | None:
    digitos = re.sub(r"\D", "", valor or "")
    return digitos[-10:] if len(digitos) >= 10 else None


# --------------------------------------------------------------------- envío
async def cobrar_al_celular(
    db: AsyncSession,
    cliente: ClienteModel,
    telefono: str,
    http: httpx.AsyncClient | None = None,
    origen: str = "whatsapp",
) -> dict:
    """Manda el cobro CoDi por el total que debe el cliente al celular indicado."""
    from src.application.services.billing_service import BillingService

    config = await config_pagos(db)
    clave = (config.pey_api_key or "").strip()
    if not config.codi_activo or not clave:
        return {"error": "El cobro por CoDi no está activo; da los datos para transferencia."}
    numero = _telefono_10(telefono)
    if not numero:
        return {"error": "Hace falta un celular de 10 dígitos para mandar el cobro."}

    estado = await BillingService(db).estado_cuenta_cliente(cliente.id)
    total = _monto(estado.get("total"))
    if total <= 0:
        return {"error": "El cliente no tiene saldo pendiente."}

    reciente = (
        await db.execute(
            select(CobroCodiModel).where(
                CobroCodiModel.cliente_id == cliente.id,
                CobroCodiModel.telefono == numero,
                CobroCodiModel.estado == "enviado",
                CobroCodiModel.monto == total,
                CobroCodiModel.creado_en >= datetime.now() - ESPERA_REENVIO,
            ).limit(1)
        )
    ).scalars().first()
    if reciente:
        return {
            "enviado": True,
            "ya_enviado_hace_un_momento": True,
            "monto": str(total),
            "celular": numero,
            "vence_en_minutos": VIGENCIA_SEGUNDOS // 60,
        }

    cobro = CobroCodiModel(
        cliente_id=cliente.id, tipo="celular", telefono=numero, monto=total,
        estado="enviado", origen=origen, creado_en=datetime.now(),
    )
    db.add(cobro)
    await db.flush()
    cobro.num_ref = str(cobro.id % 10_000_000 or 1)
    cuerpo = {
        "amount": float(total),
        "phoneNumber": numero,
        "concept": f"Internet contrato {cliente.cedula or cliente.id}"[:40],
        "expiration": VIGENCIA_SEGUNDOS,
        "numRef": cobro.num_ref,
        "metadata": {"cobro_id": cobro.id, "cliente_id": cliente.id},
    }
    cliente_http = http or httpx.AsyncClient(timeout=30)
    try:
        respuesta = await cliente_http.post(
            f"{PEY_API_URL}/codi/pn", json=cuerpo, headers={"Authorization": f"Bearer {clave}"},
        )
        datos = respuesta.json() if respuesta.content else {}
    except Exception as exc:
        logger.exception("pey no respondió al cobro CoDi %s", cobro.id)
        cobro.estado = "error"
        cobro.detalle = f"{type(exc).__name__}: {exc}"[:500]
        await db.commit()
        return {"error": "No se pudo mandar el cobro en este momento; da los datos para transferencia."}
    finally:
        if http is None:
            await cliente_http.aclose()

    if respuesta.status_code >= 400 or not datos.get("transactionId"):
        cobro.estado = "error"
        cobro.detalle = f"HTTP {respuesta.status_code}: {respuesta.text[:450]}"
        await db.commit()
        logger.warning("pey rechazó el cobro CoDi %s: %s", cobro.id, cobro.detalle)
        return {"error": "No se pudo mandar el cobro a ese número; da los datos para transferencia."}

    cobro.pey_transaction_id = str(datos["transactionId"])[:40]
    cobro.codi_id = str(datos.get("codiId") or "")[:40] or None
    await db.commit()
    return {
        "enviado": True,
        "monto": str(total),
        "celular": numero,
        "vence_en_minutos": VIGENCIA_SEGUNDOS // 60,
    }


# ------------------------------------------------------------------- webhook
def _llave_firma(secreto: str) -> bytes:
    secreto = secreto.strip()
    if secreto.startswith("whsec_"):
        return base64.b64decode(secreto[len("whsec_"):])
    try:
        return base64.b64decode(secreto, validate=True)
    except Exception:
        return secreto.encode()


def verificar_firma(secreto: str, headers, cuerpo: bytes, ahora: float | None = None) -> None:
    """Standard Webhooks: HMAC-SHA256 de "id.timestamp.cuerpo" en base64."""
    mensaje_id = headers.get("webhook-id") or ""
    marca = headers.get("webhook-timestamp") or ""
    firmas = headers.get("webhook-signature") or ""
    if not (secreto and mensaje_id and marca and firmas):
        raise FirmaInvalida("Faltan encabezados de firma")
    try:
        segundos = int(marca)
    except ValueError as exc:
        raise FirmaInvalida("Marca de tiempo inválida") from exc
    if abs((ahora or time.time()) - segundos) > TOLERANCIA_FIRMA_SEGUNDOS:
        raise FirmaInvalida("El aviso es demasiado viejo")
    firmado = f"{mensaje_id}.{marca}.".encode() + cuerpo
    esperada = base64.b64encode(hmac.new(_llave_firma(secreto), firmado, hashlib.sha256).digest()).decode()
    for parte in firmas.split():
        version, _, firma = parte.partition(",")
        if version == "v1" and hmac.compare_digest(firma, esperada):
            return
    raise FirmaInvalida("Firma incorrecta")


async def procesar_webhook(db: AsyncSession, headers, cuerpo: bytes) -> dict:
    """Aplica el pago que avisa pey. Repetir el aviso no cobra dos veces."""
    from src.application.services.billing_service import BillingService

    config = await config_pagos(db)
    verificar_firma(config.pey_webhook_secreto or "", headers, cuerpo)
    datos = json.loads(cuerpo or b"{}")
    tipo = datos.get("type")
    if tipo not in EVENTOS_PAGO:
        return {"ignorado": tipo}

    transaccion = str(datos.get("transactionId") or "")[:40]
    monto = _monto(datos.get("amount"))
    metadata = datos.get("metadata") or {}
    externos = metadata.get("externalMetadata") or {}
    cobro = None
    if transaccion:
        cobro = (
            await db.execute(select(CobroCodiModel).where(CobroCodiModel.pey_transaction_id == transaccion))
        ).scalars().first()
    if cobro is None:
        cobro_id = externos.get("cobro_id") or metadata.get("cobro_id")
        if str(cobro_id or "").isdigit():
            cobro = await db.get(CobroCodiModel, int(cobro_id))
    if cobro and cobro.estado == "aplicado":
        return {"aplicado": True, "repetido": True}
    if cobro is None:
        cobro = CobroCodiModel(
            tipo="qr" if tipo.startswith("codi.qr") else "celular", monto=monto,
            estado="sin_aplicar", origen="desconocido", creado_en=datetime.now(),
        )
        db.add(cobro)
    if transaccion and not cobro.pey_transaction_id:
        cobro.pey_transaction_id = transaccion
    cobro.monto_pagado = monto
    cobro.pagado_en = (
        datetime.fromtimestamp(int(datos["datePaid"])) if str(datos.get("datePaid") or "").isdigit() else datetime.now()
    )
    cobro.num_ref = str(datos.get("numRef") or cobro.num_ref or "")[:7] or None
    rastreo = metadata.get("trackingKey")

    cliente = await db.get(ClienteModel, cobro.cliente_id) if cobro.cliente_id else None
    if not cliente or monto <= 0:
        cobro.estado = "sin_aplicar"
        cobro.detalle = "Pago CoDi sin cliente: regístralo a mano"
        await db.commit()
        await _avisar_personal(
            db,
            f"💸 *Pago CoDi sin aplicar* de ${monto:.2f} (ref. {cobro.num_ref or '-'}"
            f"{f', rastreo {rastreo}' if rastreo else ''}). No se sabe de qué cliente es: regístralo en la Terminal de Cobro.",
        )
        return {"aplicado": False, "motivo": "sin_cliente"}

    usuario = await usuario_de_sistema(db, USUARIO_CODI)
    referencia = f"CoDi {rastreo or cobro.num_ref or transaccion}"[:100]
    try:
        resultado = await BillingService(db).registrar_pago_cliente(
            cliente.id, usuario, "codi", monto, referencia=referencia,
            clave_idempotencia=f"codi:{transaccion or cobro.id}",
        )
    except ValueError as exc:
        await db.rollback()
        cobro = await _recargar(db, cobro, transaccion)
        cobro.pey_transaction_id = cobro.pey_transaction_id or transaccion or None
        cobro.estado = "sin_aplicar"
        cobro.monto_pagado = monto
        cobro.detalle = str(exc)[:500]
        await db.commit()
        await _avisar_personal(
            db,
            f"💸 *Pago CoDi sin aplicar* de ${monto:.2f} de {cliente.nombre} (contrato {cliente.cedula}): "
            f"{exc}. Revísalo en la Terminal de Cobro.",
        )
        return {"aplicado": False, "motivo": str(exc)}

    cobro = await _recargar(db, cobro, transaccion)
    cobro.estado = "aplicado"
    cobro.monto_pagado = monto
    cobro.pago_ids = ",".join(str(i) for i in resultado.get("pago_ids") or [])[:255]
    cobro.detalle = None
    await db.commit()
    return {"aplicado": True, "pago_ids": resultado.get("pago_ids")}


async def _recargar(db: AsyncSession, cobro: CobroCodiModel, transaccion: str) -> CobroCodiModel:
    """El cobro después de un commit o rollback (el de memoria queda vencido)."""
    if cobro.id:
        actual = await db.get(CobroCodiModel, cobro.id)
        if actual:
            await db.refresh(actual)
            return actual
    if transaccion:
        actual = (
            await db.execute(select(CobroCodiModel).where(CobroCodiModel.pey_transaction_id == transaccion))
        ).scalars().first()
        if actual:
            return actual
    db.add(cobro)
    return cobro


async def _avisar_personal(db: AsyncSession, texto: str) -> None:
    """A los teléfonos de alerta, por la cola durable de WhatsApp."""
    from src.infrastructure.whatsapp_client import whatsapp_queue

    config = await db.get(ConfiguracionSistema, 1)
    for numero in [n.strip() for n in (getattr(config, "telefonos_alerta", "") or "").split(",") if n.strip()]:
        try:
            mensaje = MensajeChatModel(
                cliente_id=None, telefono=numero, direccion="salida", tipo_mensaje="texto",
                tipo_evento="alerta_pago_codi", leido=True, ack=0, estado_envio="pendiente", mensaje=texto,
            )
            db.add(mensaje)
            await db.commit()
            await whatsapp_queue.agregar_tarea({"mensaje_chat_id": mensaje.id, "intervalo": 0, "tipo_evento": "alerta_pago_codi"})
        except Exception:
            logger.exception("No se pudo avisar del pago CoDi a %s", numero)
