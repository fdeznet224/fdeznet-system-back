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


def contratos_de_prueba(config) -> set[str]:
    return {c.strip().upper() for c in (getattr(config, "codi_solo_contratos", None) or "").split(",") if c.strip()}


def codi_para(config, cliente=None) -> bool:
    """Activo y con llave; en prueba, solo para los contratos de la lista."""
    if not (config and config.codi_activo and (config.pey_api_key or "").strip()):
        return False
    prueba = contratos_de_prueba(config)
    if not prueba:
        return True
    return bool(cliente is not None and (getattr(cliente, "cedula", None) or "").strip().upper() in prueba)


async def codi_disponible(db: AsyncSession, cliente=None) -> bool:
    return codi_para(await db.get(ConfiguracionCorreoBancoModel, 1), cliente)


def _monto(valor) -> Decimal:
    try:
        return Decimal(str(valor)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0.00")


def _telefono_10(valor: str | None) -> str | None:
    digitos = re.sub(r"\D", "", valor or "")
    return digitos[-10:] if len(digitos) >= 10 else None


def celular_para_codi(cliente, numero_chat: str | None = None) -> str | None:
    """A qué celular se ofrece el cobro: el que ya funcionó, el del chat o el registrado."""
    if numero_chat and "@lid" in numero_chat:
        numero_chat = None  # un LID de WhatsApp no es un teléfono
    return (
        _telefono_10(getattr(cliente, "telefono_codi", None))
        or _telefono_10(numero_chat)
        or _telefono_10(getattr(cliente, "telefono", None))
    )


def celular_legible(numero: str | None) -> str:
    return f"{numero[:3]} {numero[3:6]} {numero[6:]}" if numero and len(numero) == 10 else (numero or "")


async def _total_a_cobrar(db: AsyncSession, cliente, meses_adelanto: int = 0) -> tuple[Decimal, int, list]:
    """Lo que debe hoy más los meses que quiere adelantar (sin facturar todavía)."""
    from src.application.services.billing_service import BillingService

    billing = BillingService(db)
    deuda = _monto((await billing.estado_cuenta_cliente(cliente.id)).get("total"))
    meses = max(0, min(int(meses_adelanto or 0), 12))
    if not meses:
        return deuda, 0, []
    cotizacion = await billing.cotizar_adelanto(cliente.id, meses)
    if cotizacion["total"] <= 0:
        return deuda, 0, []
    adelanto = [{"mes": d["mes"], "total": str(d["total"])} for d in cotizacion["detalle"]]
    return (deuda + cotizacion["total"]).quantize(Decimal("0.01")), meses, adelanto


# --------------------------------------------------------------------- envío
async def cobrar_al_celular(
    db: AsyncSession,
    cliente: ClienteModel,
    telefono: str,
    http: httpx.AsyncClient | None = None,
    origen: str = "whatsapp",
    meses_adelanto: int = 0,
) -> dict:
    """Manda el cobro CoDi por lo que debe (más los meses que adelanta) al celular indicado."""
    config = await config_pagos(db)
    clave = (config.pey_api_key or "").strip()
    if not codi_para(config, cliente):
        return {"error": "El cobro por CoDi no está activo; da los datos para transferencia."}
    numero = _telefono_10(telefono)
    if not numero:
        return {"error": "Hace falta un celular de 10 dígitos para mandar el cobro."}

    total, meses_adelanto, adelanto = await _total_a_cobrar(db, cliente, meses_adelanto)
    if total <= 0:
        return {"error": "El cliente no tiene saldo pendiente; si quiere adelantar meses, di cuántos."}

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
            "cobro_id": reciente.id,
            "ya_enviado_hace_un_momento": True,
            "monto": str(total),
            "celular": numero,
            "vence_en_minutos": VIGENCIA_SEGUNDOS // 60,
        }

    cobro = CobroCodiModel(
        cliente_id=cliente.id, tipo="celular", telefono=numero, monto=total,
        estado="enviado", origen=origen, creado_en=datetime.now(), meses_adelanto=meses_adelanto,
        vence_en=datetime.now() + timedelta(seconds=VIGENCIA_SEGUNDOS),
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
        "cobro_id": cobro.id,
        "monto": str(total),
        **({"incluye_adelanto": adelanto} if adelanto else {}),
        "celular": numero,
        "vence_en_minutos": VIGENCIA_SEGUNDOS // 60,
    }


# ------------------------------------------------------------------- QR
# El QR lo escanea otra persona (un familiar que le paga) o el cliente frente al técnico.
VIGENCIA_QR_CLIENTE = timedelta(hours=24)
VIGENCIA_QR_TECNICO = timedelta(minutes=30)


def imagen_qr(texto: str) -> bytes:
    """PNG del QR CoDi (se dibuja aquí con el texto que da pey)."""
    import io

    import qrcode

    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=3)
    qr.add_data(texto)
    salida = io.BytesIO()
    qr.make_image().save(salida, format="PNG")
    return salida.getvalue()


async def crear_qr(
    db: AsyncSession,
    cliente: ClienteModel,
    vigencia: timedelta = VIGENCIA_QR_CLIENTE,
    origen: str = "portal",
    http: httpx.AsyncClient | None = None,
    meses_adelanto: int = 0,
) -> dict:
    """QR CoDi por lo que debe (más los meses que adelanta); si hay uno vigente igual, se reusa."""
    config = await config_pagos(db)
    if not codi_para(config, cliente):
        return {"error": "El cobro por CoDi no está activo."}
    total, meses_adelanto, _adelanto = await _total_a_cobrar(db, cliente, meses_adelanto)
    if total <= 0:
        return {"error": "El cliente no tiene saldo pendiente."}

    ahora = datetime.now()
    # Se reusa si le queda al menos la mitad de su tiempo: un QR nuevo por cada vez
    # que lo piden deja varios vivos y el riesgo de que lo paguen dos veces.
    vigente = (
        await db.execute(
            select(CobroCodiModel).where(
                CobroCodiModel.cliente_id == cliente.id,
                CobroCodiModel.tipo == "qr",
                CobroCodiModel.estado == "enviado",
                CobroCodiModel.monto == total,
                CobroCodiModel.meses_adelanto == meses_adelanto,
                CobroCodiModel.qr_texto.is_not(None),
                CobroCodiModel.vence_en >= ahora + vigencia / 2,
            ).order_by(CobroCodiModel.id.desc()).limit(1)
        )
    ).scalars().first()
    if vigente:
        return _datos_qr(vigente)

    cobro = CobroCodiModel(
        cliente_id=cliente.id, tipo="qr", monto=total, estado="enviado", origen=origen,
        creado_en=ahora, vence_en=ahora + vigencia, meses_adelanto=meses_adelanto,
    )
    db.add(cobro)
    await db.flush()
    cobro.num_ref = str(cobro.id % 10_000_000 or 1)
    cuerpo = {
        "amount": float(total),
        "concept": f"Internet contrato {cliente.cedula or cliente.id}"[:40],
        "expiration": int(vigencia.total_seconds()),
        "numRef": cobro.num_ref,
        "metadata": {"cobro_id": cobro.id, "cliente_id": cliente.id},
    }
    cliente_http = http or httpx.AsyncClient(timeout=30)
    try:
        respuesta = await cliente_http.post(
            f"{PEY_API_URL}/codi/qr", json=cuerpo,
            headers={"Authorization": f"Bearer {(config.pey_api_key or '').strip()}"},
        )
        datos = respuesta.json() if respuesta.content else {}
    except Exception as exc:
        logger.exception("pey no respondió al QR CoDi %s", cobro.id)
        cobro.estado = "error"
        cobro.detalle = f"{type(exc).__name__}: {exc}"[:500]
        await db.commit()
        return {"error": "No se pudo generar el QR en este momento."}
    finally:
        if http is None:
            await cliente_http.aclose()
    if respuesta.status_code >= 400 or not datos.get("qrString") or not datos.get("id"):
        cobro.estado = "error"
        cobro.detalle = f"HTTP {respuesta.status_code}: {respuesta.text[:450]}"
        await db.commit()
        logger.warning("pey rechazó el QR CoDi %s: %s", cobro.id, cobro.detalle)
        return {"error": "No se pudo generar el QR en este momento."}
    cobro.codi_id = str(datos["id"])[:40]
    cobro.qr_texto = str(datos["qrString"])
    await db.commit()
    return _datos_qr(cobro)


def _datos_qr(cobro: CobroCodiModel) -> dict:
    return {
        "cobro_id": cobro.id,
        "monto": str(_monto(cobro.monto)),
        "vence_en": cobro.vence_en.isoformat(timespec="minutes") if cobro.vence_en else None,
        "qr_png": base64.b64encode(imagen_qr(cobro.qr_texto)).decode(),
    }


async def estado_cobro(db: AsyncSession, cobro_id: int, cliente_id: int | None = None) -> dict | None:
    """Cómo va un cobro; antes pregunta a pey por si el aviso no llegó."""
    cobro = await db.get(CobroCodiModel, cobro_id)
    if cobro is None or (cliente_id is not None and cobro.cliente_id != cliente_id):
        return None
    if cobro.estado == "enviado":
        await revisar_cobros_pendientes(db, cliente_id=cobro.cliente_id)
        cobro = await db.get(CobroCodiModel, cobro_id)
        await db.refresh(cobro)
    pagado = cobro.estado == "aplicado" or (
        cobro.tipo == "qr" and bool(
            (await db.execute(
                select(CobroCodiModel.id).where(
                    CobroCodiModel.codi_id == cobro.codi_id, CobroCodiModel.estado == "aplicado",
                ).limit(1)
            )).scalar_one_or_none()
        )
    )
    return {"cobro_id": cobro.id, "estado": "pagado" if pagado else cobro.estado, "monto": str(_monto(cobro.monto))}


# ------------------------------------------------------- revisión de respaldo
# Si el aviso de pey no llega (servidor reiniciando, internet caído), el pago se
# busca en pey y se aplica igual. El aviso y esta revisión nunca lo aplican dos veces.
MARGEN_VENCIMIENTO = timedelta(minutes=15)
REVISAR_HASTA = timedelta(days=2)


async def revisar_cobros_pendientes(
    db: AsyncSession, cliente_id: int | None = None, http: httpx.AsyncClient | None = None,
) -> int:
    """Pregunta a pey por los cobros sin pagar; aplica los pagados y marca los vencidos."""
    config = await config_pagos(db)
    clave = (config.pey_api_key or "").strip()
    if not clave:
        return 0
    ahora = datetime.now()
    filtros = [
        CobroCodiModel.estado == "enviado",
        CobroCodiModel.creado_en >= ahora - REVISAR_HASTA,
        CobroCodiModel.creado_en <= ahora - timedelta(seconds=30),
    ]
    if cliente_id is not None:
        filtros.append(CobroCodiModel.cliente_id == cliente_id)
    cobros = (await db.execute(select(CobroCodiModel).where(*filtros).order_by(CobroCodiModel.id))).scalars().all()
    pendientes = [(c.id, c.tipo, c.pey_transaction_id, c.codi_id, c.vence_en, c.creado_en) for c in cobros]
    if not pendientes:
        return 0

    aplicados = 0
    cliente_http = http or httpx.AsyncClient(timeout=20)
    cabeceras = {"Authorization": f"Bearer {clave}"}
    try:
        for cobro_id, tipo, transaccion, codi_id, vence_en, creado_en in pendientes:
            try:
                if tipo == "qr" and codi_id:
                    respuesta = await cliente_http.get(f"{PEY_API_URL}/transaction/codi/qr/{codi_id}", headers=cabeceras)
                    pagos = (respuesta.json() or {}).get("items") or [] if respuesta.status_code == 200 else []
                    for pago in pagos:
                        if pago.get("status") == "PAID" and pago.get("id"):
                            resultado = await aplicar_pago_codi(
                                db, es_qr=True, transaccion=str(pago["id"])[:40], monto=_monto(pago.get("amount")),
                                fecha_pagado=pago.get("datePaid"), metadata=pago.get("metadata") or {},
                                codi_qr_id=codi_id,
                            )
                            aplicados += int(bool(resultado.get("aplicado") and not resultado.get("repetido")))
                elif transaccion:
                    respuesta = await cliente_http.get(f"{PEY_API_URL}/transaction/{transaccion}", headers=cabeceras)
                    pago = respuesta.json() if respuesta.status_code == 200 and respuesta.content else {}
                    if pago.get("status") == "PAID":
                        resultado = await aplicar_pago_codi(
                            db, es_qr=False, transaccion=transaccion, monto=_monto(pago.get("amount")),
                            fecha_pagado=pago.get("datePaid"), metadata=pago.get("metadata") or {},
                        )
                        aplicados += int(bool(resultado.get("aplicado") and not resultado.get("repetido")))
                        continue
                else:
                    continue
            except Exception:
                logger.exception("No se pudo revisar en pey el cobro CoDi %s", cobro_id)
                await db.rollback()
                continue
            limite = (vence_en or creado_en + timedelta(seconds=VIGENCIA_SEGUNDOS)) + MARGEN_VENCIMIENTO
            if ahora > limite:
                cobro = await db.get(CobroCodiModel, cobro_id)
                if cobro is not None and cobro.estado == "enviado":
                    cobro.estado = "vencido"
                    await db.commit()
    finally:
        if http is None:
            await cliente_http.aclose()
    if aplicados:
        logger.info("Revisión CoDi: %s pago(s) aplicados sin aviso de pey", aplicados)
    return aplicados


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
    config = await config_pagos(db)
    verificar_firma(config.pey_webhook_secreto or "", headers, cuerpo)
    datos = json.loads(cuerpo or b"{}")
    tipo = datos.get("type")
    if tipo not in EVENTOS_PAGO:
        return {"ignorado": tipo}
    return await aplicar_pago_codi(
        db,
        es_qr=tipo.startswith("codi.qr"),
        transaccion=str(datos.get("transactionId") or "")[:40],
        monto=_monto(datos.get("amount")),
        fecha_pagado=datos.get("datePaid"),
        num_ref=datos.get("numRef"),
        metadata=datos.get("metadata") or {},
        codi_qr_id=datos.get("codiQrId"),
    )


async def aplicar_pago_codi(
    db: AsyncSession,
    *,
    es_qr: bool,
    transaccion: str,
    monto: Decimal,
    fecha_pagado=None,
    num_ref=None,
    metadata: dict | None = None,
    codi_qr_id: str | None = None,
) -> dict:
    """Aplica un pago CoDi (del aviso de pey o de la revisión de respaldo); nunca dos veces."""
    from src.application.services.billing_service import BillingService

    metadata = metadata or {}
    externos = metadata.get("externalMetadata") or {}
    cobro = None
    if transaccion:
        cobro = (
            await db.execute(select(CobroCodiModel).where(CobroCodiModel.pey_transaction_id == transaccion))
        ).scalars().first()
        if cobro and cobro.estado == "aplicado":
            return {"aplicado": True, "repetido": True}
    if cobro is None:
        if es_qr and codi_qr_id:
            cobro = (
                await db.execute(
                    select(CobroCodiModel).where(CobroCodiModel.codi_id == str(codi_qr_id)[:40])
                    .order_by(CobroCodiModel.id).limit(1)
                )
            ).scalars().first()
        cobro_id = externos.get("cobro_id") or metadata.get("cobro_id")
        if cobro is None and str(cobro_id or "").isdigit():
            cobro = await db.get(CobroCodiModel, int(cobro_id))
        if cobro is not None and cobro.pey_transaction_id and cobro.pey_transaction_id != transaccion:
            if not es_qr:
                return {"aplicado": False, "motivo": "otra_transaccion"}
            # Un QR se puede pagar más de una vez: cada pago es su propio registro.
            cobro = CobroCodiModel(
                cliente_id=cobro.cliente_id, tipo="qr", codi_id=cobro.codi_id, monto=cobro.monto,
                estado="enviado", origen=cobro.origen, creado_en=datetime.now(), vence_en=cobro.vence_en,
                meses_adelanto=cobro.meses_adelanto,
            )
            db.add(cobro)
    if cobro is None:
        cobro = CobroCodiModel(
            tipo="qr" if es_qr else "celular", monto=monto,
            estado="sin_aplicar", origen="desconocido", creado_en=datetime.now(),
        )
        db.add(cobro)
    if transaccion and not cobro.pey_transaction_id:
        cobro.pey_transaction_id = transaccion
    cobro.monto_pagado = monto
    cobro.pagado_en = (
        datetime.fromtimestamp(int(fecha_pagado)) if str(fecha_pagado or "").isdigit() else datetime.now()
    )
    cobro.num_ref = str(num_ref or cobro.num_ref or "")[:7] or None
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
        resultado = await BillingService(db).registrar_pago_con_adelanto(
            cliente.id, usuario, "codi", monto, meses_adelanto=cobro.meses_adelanto or 0,
            referencia=referencia, clave_idempotencia=f"codi:{transaccion or cobro.id}",
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
    # Ese número sí tiene CoDi en su banco: la próxima vez se le ofrece ese.
    if cobro.tipo == "celular" and cobro.telefono:
        cliente = await db.get(ClienteModel, cobro.cliente_id)
        if cliente is not None:
            cliente.telefono_codi = cobro.telefono
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
