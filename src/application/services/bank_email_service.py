import asyncio
import base64
import hashlib
import imaplib
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email import policy
from email.header import decode_header, make_header
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.models import (
    ComprobantePagoRevisionModel,
    ConfiguracionCorreoBancoModel,
    FacturaModel,
    PagoAutovalidadoModel,
    TransaccionCorreoBancoModel,
    UsuarioModel,
)
from src.application.services.billing_service import BillingService


class BankEmailError(RuntimeError):
    pass


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data.strip())

    def text(self) -> str:
        return " ".join(self.parts)


@dataclass(frozen=True)
class ParsedBankEmail:
    message_id: str
    uid: str | None
    sender: str
    subject: str
    received_at: datetime | None
    amount: Decimal | None
    reference: str | None
    concept: str | None
    authenticated: bool
    authentication_detail: str
    content_hash: str


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def normalize_reference(value: str | None) -> str | None:
    normalized = re.sub(r"[^A-Z0-9]", "", (value or "").upper())
    return normalized if len(normalized) >= 6 else None


def normalize_reference_for_match(value: str | None) -> str | None:
    normalized = normalize_reference(value)
    if not normalized:
        return None
    # OCR confunde con frecuencia estas letras con dígitos en claves bancarias.
    # Se normaliza la referencia completa; nunca se compara solo un fragmento.
    return normalized.translate(str.maketrans({"O": "0", "I": "1"}))


def _message_text(message) -> str:
    plain: list[str] = []
    html: list[str] = []
    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        if part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type()
        if content_type not in {"text/plain", "text/html"}:
            continue
        try:
            content = part.get_content()
        except Exception:
            payload = part.get_payload(decode=True) or b""
            content = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if content_type == "text/plain":
            plain.append(str(content))
        else:
            html.append(str(content))
    if plain:
        return " ".join(plain)
    parser = _TextExtractor()
    parser.feed(" ".join(html))
    return unescape(parser.text())


def _parse_amount(text: str) -> Decimal | None:
    number = r"((?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]{1,2})?|[0-9]+,[0-9]{1,2})"
    patterns = [
        rf"(?:recibiste|recibido|abono|dep[oó]sito|transferencia|monto|importe|cantidad|total)[^0-9]{{0,60}}\$?\s*{number}",
        rf"\$\s*{number}\s*(?:mxn|m\.?n\.?|pesos)?",
        rf"{number}\s*(?:mxn|m\.?n\.?|pesos)",
    ]
    normalized_text = " ".join(text.split())
    for pattern in patterns:
        match = re.search(pattern, normalized_text, flags=re.IGNORECASE)
        if not match:
            continue
        try:
            raw_amount = match.group(1)
            if "," in raw_amount and "." in raw_amount:
                raw_amount = raw_amount.replace(",", "")
            elif "," in raw_amount:
                integer, decimals = raw_amount.rsplit(",", 1)
                raw_amount = (
                    f"{integer}.{decimals}"
                    if len(decimals) <= 2
                    else f"{integer}{decimals}"
                )
            amount = Decimal(raw_amount).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            continue
        if amount > 0:
            return amount
    return None


def _parse_reference(text: str) -> str | None:
    patterns = [
        r"clave\s+de\s+rastreo\s*[:#-]?\s*([A-Z0-9-]{6,60})",
        r"(?:folio|referencia|ref\.?|autorizaci[oó]n)\s*[:#-]?\s*([A-Z0-9-]{6,60})",
        r"(?:n[uú]mero|no\.?)\s+de\s+(?:operaci[oó]n|movimiento)\s*[:#-]?\s*([A-Z0-9-]{6,60})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return normalize_reference(match.group(1))
    return None


def _parse_concept(text: str) -> str | None:
    match = re.search(
        r"(?:concepto|motivo|descripci[oó]n)\s*[:#-]?\s*([^\r\n]{1,120})",
        text,
        flags=re.IGNORECASE,
    )
    return " ".join(match.group(1).split())[:255] if match else None


def parse_bank_email(
    raw_message: bytes,
    *,
    uid: str | None,
    allowed_senders: set[str],
    require_dkim: bool = True,
) -> ParsedBankEmail:
    message = BytesParser(policy=policy.default).parsebytes(raw_message)
    sender = parseaddr(_decode(message.get("From")))[1].strip().casefold()
    subject = _decode(message.get("Subject")).strip()
    body = _message_text(message)
    searchable = f"{subject}\n{body}"
    content_hash = hashlib.sha256(raw_message).hexdigest()
    message_id = (message.get("Message-ID") or "").strip() or f"sha256:{content_hash}"

    received_at = None
    if message.get("Date"):
        try:
            parsed_date = parsedate_to_datetime(message.get("Date"))
            if parsed_date.tzinfo is None:
                parsed_date = parsed_date.replace(tzinfo=timezone.utc)
            received_at = parsed_date.astimezone(timezone.utc).replace(tzinfo=None)
        except (TypeError, ValueError, OverflowError):
            received_at = None

    authentication_headers = message.get_all("Authentication-Results", [])
    gmail_authentication = [
        header
        for header in authentication_headers
        if re.match(r"\s*(?:mx\.)?google\.com\s*;", header, flags=re.IGNORECASE)
    ]
    authentication = " ".join(gmail_authentication)
    authentication_lower = authentication.casefold()
    sender_allowed = sender in allowed_senders
    sender_domain = sender.rsplit("@", 1)[-1] if "@" in sender else ""
    dkim_domains = re.findall(
        r"\bdkim\s*=\s*pass\b[^;]*?\bheader\.(?:d|i)\s*=\s*([^\s;]+)",
        authentication_lower,
    )
    dmarc_domains = re.findall(
        r"\bdmarc\s*=\s*pass\b[^;]*?\bheader\.from\s*=\s*([^\s;]+)",
        authentication_lower,
    )

    def aligned(domain: str) -> bool:
        clean = domain.strip("<>.@\"")
        return bool(
            sender_domain
            and (
                clean == sender_domain
                or sender_domain.endswith(f".{clean}")
                or clean.endswith(f".{sender_domain}")
            )
        )

    dkim_passed = any(aligned(domain) for domain in dkim_domains)
    dmarc_passed = any(aligned(domain) for domain in dmarc_domains)
    spf_passed = bool(re.search(r"\bspf\s*=\s*pass\b", authentication_lower))
    authenticated = sender_allowed and (
        (dkim_passed and dmarc_passed) if require_dkim else True
    )
    detail = (
        f"gmail_auth={'ok' if gmail_authentication else 'ausente'}; "
        f"sender={'ok' if sender_allowed else 'rechazado'}; "
        f"dkim={'pass' if dkim_passed else 'sin_pass'}; "
        f"dmarc={'pass' if dmarc_passed else 'sin_pass'}; "
        f"spf={'pass' if spf_passed else 'sin_pass'}"
    )

    return ParsedBankEmail(
        message_id=message_id[:255],
        uid=uid,
        sender=sender[:255],
        subject=subject[:500],
        received_at=received_at,
        amount=_parse_amount(searchable),
        reference=_parse_reference(searchable),
        concept=_parse_concept(searchable),
        authenticated=authenticated,
        authentication_detail=detail[:500],
        content_hash=content_hash,
    )


def _fernet():
    from cryptography.fernet import Fernet

    secret = os.getenv("SECRET_KEY", "").strip()
    if not secret:
        raise BankEmailError("SECRET_KEY no está configurada")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def encrypt_mail_secret(secret: str) -> str:
    value = "".join(secret.split())
    if len(value) < 16:
        raise BankEmailError("La contraseña de aplicación de Gmail debe tener 16 caracteres")
    return _fernet().encrypt(value.encode()).decode()


def decrypt_mail_secret(token: str) -> str:
    from cryptography.fernet import InvalidToken

    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError) as exc:
        raise BankEmailError("No se pudo descifrar la credencial de Gmail") from exc


def _read_gmail_messages(
    *,
    address: str,
    app_password: str,
    folder: str,
    since: datetime,
    last_uid: str | None,
    limit: int = 200,
) -> list[tuple[str, bytes]]:
    connection = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=30)
    try:
        connection.login(address, app_password)
        status, _ = connection.select(folder or "INBOX", readonly=True)
        if status != "OK":
            raise BankEmailError("Gmail no permitió abrir la carpeta configurada")
        criteria: list[str] = ["SINCE", since.strftime("%d-%b-%Y")]
        if last_uid and last_uid.isdigit():
            criteria.extend(["UID", f"{int(last_uid) + 1}:*"])
        status, data = connection.uid("search", None, *criteria)
        if status != "OK":
            raise BankEmailError("Gmail no permitió buscar mensajes")
        uids = (data[0] or b"").split()[-limit:]
        messages: list[tuple[str, bytes]] = []
        for uid_bytes in uids:
            status, fetched = connection.uid("fetch", uid_bytes, "(BODY.PEEK[])")
            if status != "OK" or not fetched:
                continue
            raw = next(
                (item[1] for item in fetched if isinstance(item, tuple) and len(item) > 1),
                None,
            )
            if raw:
                messages.append((uid_bytes.decode(), raw))
        return messages
    except imaplib.IMAP4.error as exc:
        raise BankEmailError("Gmail rechazó la cuenta o la contraseña de aplicación") from exc
    finally:
        try:
            connection.logout()
        except Exception:
            pass


class BankEmailService:
    @staticmethod
    async def get_config(db: AsyncSession) -> ConfiguracionCorreoBancoModel:
        config = await db.get(ConfiguracionCorreoBancoModel, 1)
        if config is None:
            config = ConfiguracionCorreoBancoModel(id=1)
            db.add(config)
            await db.flush()
        return config

    @staticmethod
    def allowed_senders(config: ConfiguracionCorreoBancoModel) -> set[str]:
        return {
            item.strip().casefold()
            for item in (config.remitente_permitido or "").split(",")
            if item.strip()
        }

    async def test_connection(
        self,
        *,
        address: str,
        app_password: str,
        folder: str = "INBOX",
    ) -> None:
        await asyncio.to_thread(
            _read_gmail_messages,
            address=address,
            app_password="".join(app_password.split()),
            folder=folder,
            since=datetime.utcnow() - timedelta(days=1),
            last_uid=None,
            limit=1,
        )

    async def sync(self, db: AsyncSession) -> dict:
        config = await self.get_config(db)
        if not config.activo:
            await db.commit()
            return {"status": "disabled", "nuevos": 0, "validos": 0}
        if not config.correo or not config.secreto_cifrado:
            raise BankEmailError("Completa la cuenta y la contraseña de aplicación de Gmail")
        senders = self.allowed_senders(config)
        if not senders:
            raise BankEmailError("Configura al menos un remitente bancario permitido")

        try:
            messages = await asyncio.to_thread(
                _read_gmail_messages,
                address=config.correo,
                app_password=decrypt_mail_secret(config.secreto_cifrado),
                folder=config.carpeta or "INBOX",
                since=datetime.utcnow() - timedelta(days=max(1, config.ventana_dias)),
                last_uid=config.ultimo_uid,
            )
            created = 0
            valid = 0
            max_uid = int(config.ultimo_uid or 0)
            for uid, raw in messages:
                max_uid = max(max_uid, int(uid)) if uid.isdigit() else max_uid
                parsed = parse_bank_email(
                    raw,
                    uid=uid,
                    allowed_senders=senders,
                    require_dkim=config.requiere_dkim,
                )
                exists = await db.scalar(
                    select(TransaccionCorreoBancoModel.id).where(
                        TransaccionCorreoBancoModel.correo_message_id == parsed.message_id
                    )
                )
                if exists:
                    continue
                subject_matches = (
                    not config.asunto_filtro
                    or config.asunto_filtro.casefold() in parsed.subject.casefold()
                )
                usable = bool(
                    parsed.authenticated
                    and parsed.amount
                    and parsed.reference
                    and subject_matches
                )
                db.add(
                    TransaccionCorreoBancoModel(
                        correo_message_id=parsed.message_id,
                        uid_buzon=parsed.uid,
                        remitente=parsed.sender,
                        asunto=parsed.subject,
                        fecha_correo=parsed.received_at,
                        monto=parsed.amount,
                        referencia=parsed.reference,
                        concepto=parsed.concept,
                        autenticado=parsed.authenticated,
                        detalle_autenticacion=parsed.authentication_detail,
                        contenido_hash=parsed.content_hash,
                        estado="disponible" if usable else "ignorada",
                    )
                )
                created += 1
                valid += int(usable)
            config.ultimo_uid = str(max_uid) if max_uid else config.ultimo_uid
            config.ultima_revision = datetime.utcnow()
            config.ultimo_error = None
            await db.commit()
            return {"status": "ok", "nuevos": created, "validos": valid}
        except Exception as exc:
            await db.rollback()
            config = await self.get_config(db)
            config.ultima_revision = datetime.utcnow()
            config.ultimo_error = str(exc)[:500]
            await db.commit()
            if isinstance(exc, BankEmailError):
                raise
            raise BankEmailError(
                f"No se pudo sincronizar Gmail: {type(exc).__name__}"
            ) from exc

    async def find_match(
        self,
        db: AsyncSession,
        revision: ComprobantePagoRevisionModel,
    ) -> tuple[TransaccionCorreoBancoModel | None, str]:
        config = await self.get_config(db)
        if not config.activo:
            return None, "correo_bancario_no_configurado"
        reference = normalize_reference(revision.folio_detectado)
        amount = Decimal(revision.monto_detectado or 0).quantize(Decimal("0.01"))
        if not reference or amount <= 0:
            return None, "comprobante_sin_referencia_o_monto"
        tolerance = Decimal(config.tolerancia_monto or 0)
        earliest = revision.fecha_recepcion - timedelta(days=max(1, config.ventana_dias))
        latest = revision.fecha_recepcion + timedelta(days=1)
        amount_and_date_candidates = (
            await db.execute(
                select(TransaccionCorreoBancoModel)
                .where(
                    TransaccionCorreoBancoModel.estado == "disponible",
                    TransaccionCorreoBancoModel.autenticado.is_(True),
                    TransaccionCorreoBancoModel.monto >= amount - tolerance,
                    TransaccionCorreoBancoModel.monto <= amount + tolerance,
                    TransaccionCorreoBancoModel.fecha_correo >= earliest,
                    TransaccionCorreoBancoModel.fecha_correo <= latest,
                )
                .order_by(TransaccionCorreoBancoModel.fecha_correo.desc())
                .with_for_update()
            )
        ).scalars().all()
        expected_reference = normalize_reference_for_match(reference)
        candidates = [
            transaction
            for transaction in amount_and_date_candidates
            if normalize_reference_for_match(transaction.referencia)
            == expected_reference
        ]
        if len(candidates) == 1:
            return candidates[0], "coincidencia_exacta"
        if len(candidates) > 1:
            return None, "multiples_correos_coincidentes"
        return None, "correo_bancario_no_encontrado"

    async def reconcile_revision(
        self,
        db: AsyncSession,
        revision: ComprobantePagoRevisionModel,
        *,
        operator: UsuarioModel | None = None,
    ) -> dict:
        if revision.estado in {"aprobado", "rechazado"}:
            return {"status": revision.estado, "approved": revision.estado == "aprobado"}
        config = await self.get_config(db)
        transaction = (
            await db.get(TransaccionCorreoBancoModel, revision.transaccion_correo_id)
            if revision.transaccion_correo_id
            else None
        )
        if transaction and transaction.estado in {"disponible", "procesando"}:
            reason = "coincidencia_exacta"
        else:
            transaction, reason = await self.find_match(db, revision)
        revision.motivo_revision = reason
        if transaction is None:
            await db.commit()
            return {"status": reason, "approved": False}

        revision.transaccion_correo_id = transaction.id
        if not config.auto_aprobar:
            revision.motivo_revision = "correo_confirmado_revision_manual"
            await db.commit()
            return {
                "status": "correo_confirmado_revision_manual",
                "approved": False,
                "transaction_id": transaction.id,
            }
        if not revision.cliente_id:
            revision.motivo_revision = "correo_confirmado_cliente_no_identificado"
            await db.commit()
            return {"status": revision.motivo_revision, "approved": False}

        invoice = (
            await db.get(FacturaModel, revision.factura_id)
            if revision.factura_id
            else None
        )
        if invoice is None:
            invoice = (
                await db.execute(
                    select(FacturaModel)
                    .where(
                        FacturaModel.cliente_id == revision.cliente_id,
                        FacturaModel.estado.in_(["pendiente", "vencida"]),
                        FacturaModel.saldo_pendiente > 0,
                    )
                    .order_by(FacturaModel.fecha_vencimiento.asc())
                    .limit(1)
                )
            ).scalars().first()
        if invoice is None:
            revision.motivo_revision = "correo_confirmado_sin_factura_pendiente"
            await db.commit()
            return {"status": revision.motivo_revision, "approved": False}

        amount = Decimal(transaction.monto or 0).quantize(Decimal("0.01"))
        debt = Decimal(invoice.saldo_pendiente or 0).quantize(Decimal("0.01"))
        if amount < debt:
            revision.motivo_revision = "correo_confirmado_monto_insuficiente"
            revision.notas_revision = f"Correo ${amount}; deuda ${debt}"
            await db.commit()
            return {"status": revision.motivo_revision, "approved": False}

        if operator is None:
            operator = (
                await db.execute(
                    select(UsuarioModel)
                    .where(
                        UsuarioModel.rol == "admin",
                        UsuarioModel.activo.is_(True),
                    )
                    .order_by(UsuarioModel.id.asc())
                    .limit(1)
                )
            ).scalars().first()
        if operator is None:
            revision.motivo_revision = "sin_administrador_activo"
            await db.commit()
            return {"status": revision.motivo_revision, "approved": False}

        revision.estado = "procesando"
        revision.factura_id = invoice.id
        transaction.estado = "procesando"
        await db.flush()
        result = await BillingService(db).registrar_pago_completo(
            factura_id=invoice.id,
            usuario_operador=operator,
            metodo_pago="autovalidado",
            monto=amount,
            referencia=transaction.referencia,
            clave_idempotencia=f"correo-banco:{transaction.id}",
        )
        await db.refresh(revision)
        existing_auto_payment = await db.scalar(
            select(PagoAutovalidadoModel.id).where(
                PagoAutovalidadoModel.folio_banco == transaction.referencia
            )
        )
        if not existing_auto_payment:
            db.add(
                PagoAutovalidadoModel(
                    cliente_id=revision.cliente_id,
                    monto=amount,
                    folio_banco=transaction.referencia,
                    banco_emisor="correo_bancario",
                    fecha_pago_banco=(
                        transaction.fecha_correo.isoformat()
                        if transaction.fecha_correo
                        else None
                    ),
                    whatsapp_remitente=revision.telefono,
                )
            )
        revision.estado = "aprobado"
        revision.pago_id = result.get("pago_id")
        revision.motivo_revision = "correo_bancario_confirmado"
        revision.fecha_revision = datetime.utcnow()
        transaction.estado = "conciliada"
        transaction.pago_id = result.get("pago_id")
        transaction.conciliada_en = datetime.utcnow()
        await db.commit()
        return {
            "status": "aprobado",
            "approved": True,
            "transaction_id": transaction.id,
            **result,
        }

    async def reconcile_pending(self, db: AsyncSession, limit: int = 30) -> dict:
        config = await self.get_config(db)
        if not config.activo:
            return {"processed": 0, "approved": 0}
        revisions = (
            await db.execute(
                select(ComprobantePagoRevisionModel)
                .where(
                    ComprobantePagoRevisionModel.estado == "pendiente",
                    ComprobantePagoRevisionModel.cliente_id.is_not(None),
                    ComprobantePagoRevisionModel.monto_detectado.is_not(None),
                    ComprobantePagoRevisionModel.folio_detectado.is_not(None),
                )
                .order_by(ComprobantePagoRevisionModel.fecha_recepcion.asc())
                .limit(limit)
            )
        ).scalars().all()
        approved = 0
        for revision in revisions:
            try:
                result = await self.reconcile_revision(db, revision)
                approved += int(result.get("approved", False))
            except Exception as exc:
                await db.rollback()
                current = await db.get(ComprobantePagoRevisionModel, revision.id)
                if current and current.estado not in {"aprobado", "rechazado"}:
                    current.estado = "pendiente"
                    current.motivo_revision = "error_conciliacion_correo"
                    current.notas_revision = str(exc)[:1000]
                    await db.commit()
        return {"processed": len(revisions), "approved": approved}
