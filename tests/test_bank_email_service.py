from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from src.application.services.bank_email_service import (
    BankEmailService,
    decrypt_mail_secret,
    encrypt_mail_secret,
    normalize_reference,
    normalize_reference_for_match,
    parse_bank_email,
)
from src.interfaces.api.bank_email import _normalize_senders


def _email(authentication_results: str, sender: str = "avisos@banco.example") -> bytes:
    return (
        f"From: Banco <{sender}>\r\n"
        "To: negocio@gmail.com\r\n"
        "Subject: Recibiste una transferencia\r\n"
        "Date: Thu, 10 Sep 2026 02:15:00 +0000\r\n"
        "Message-ID: <movimiento-123@banco.example>\r\n"
        f"Authentication-Results: mx.google.com; {authentication_results}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        "Monto recibido: $1,250.00 MXN\r\n"
        "Clave de rastreo: AZT-9081726354\r\n"
        "Concepto: CONTRATO 329B\r\n"
    ).encode()


def test_parse_authenticated_bank_email():
    parsed = parse_bank_email(
        _email(
            "dkim=pass header.d=banco.example; "
            "dmarc=pass header.from=banco.example; spf=pass"
        ),
        uid="42",
        allowed_senders={"avisos@banco.example"},
    )

    assert parsed.authenticated is True
    assert parsed.amount == Decimal("1250.00")
    assert parsed.reference == "AZT9081726354"
    assert parsed.concept == "CONTRATO 329B"
    assert parsed.uid == "42"


def test_parse_integer_amount_used_by_banco_azteca():
    raw = _email(
        "dkim=pass header.d=banco.example; "
        "dmarc=pass header.from=banco.example; spf=pass"
    ).replace(b"$1,250.00 MXN", b"$300 MXN")

    parsed = parse_bank_email(
        raw,
        uid="47",
        allowed_senders={"avisos@banco.example"},
    )

    assert parsed.amount == Decimal("300.00")


def test_rejects_spoofed_sender_even_with_matching_content():
    parsed = parse_bank_email(
        _email(
            "dkim=pass header.d=fraude.example; "
            "dmarc=pass header.from=fraude.example; spf=pass",
            sender="fraude@example.net",
        ),
        uid="43",
        allowed_senders={"avisos@banco.example"},
    )

    assert parsed.authenticated is False
    assert "sender=rechazado" in parsed.authentication_detail


def test_requires_dkim_and_dmarc_by_default():
    parsed = parse_bank_email(
        _email("spf=pass"),
        uid="44",
        allowed_senders={"avisos@banco.example"},
    )

    assert parsed.authenticated is False


def test_rejects_forged_authentication_results_not_added_by_gmail():
    raw = _email(
        "dkim=pass header.d=banco.example; "
        "dmarc=pass header.from=banco.example; spf=pass"
    ).replace(b"mx.google.com", b"attacker.example")

    parsed = parse_bank_email(
        raw,
        uid="45",
        allowed_senders={"avisos@banco.example"},
    )

    assert parsed.authenticated is False
    assert "gmail_auth=ausente" in parsed.authentication_detail


def test_accepts_gmail_dkim_header_identity_format():
    parsed = parse_bank_email(
        _email(
            "dkim=pass header.i=@avisos.banco.example; "
            "dmarc=pass header.from=banco.example; spf=pass"
        ),
        uid="46",
        allowed_senders={"avisos@banco.example"},
    )

    assert parsed.authenticated is True


def test_encrypts_gmail_app_password(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "test-secret-that-is-not-used-in-production")

    encrypted = encrypt_mail_secret("abcd efgh ijkl mnop")

    assert "abcdefghijklmnop" not in encrypted
    assert decrypt_mail_secret(encrypted) == "abcdefghijklmnop"


def test_normalize_reference_removes_visual_separators():
    assert normalize_reference(" azt-9081 726354 ") == "AZT9081726354"
    assert normalize_reference("123") is None


def test_reference_match_normalizes_only_common_ocr_confusions():
    assert (
        normalize_reference_for_match("12O4-I678-O901")
        == normalize_reference_for_match("1204-1678-0901")
    )
    assert (
        normalize_reference_for_match("1204-1678-0901")
        != normalize_reference_for_match("1204-1678-0902")
    )


def test_transaction_requires_auth_reference_amount_and_datetime():
    now = datetime(2026, 9, 10, 10, 0)
    config = SimpleNamespace(
        activo=True,
        tolerancia_monto=Decimal("0.00"),
        ventana_dias=3,
    )
    revision = SimpleNamespace(
        folio_detectado="12O4-I678-O901",
        monto_detectado=Decimal("300.00"),
        fecha_recepcion=now,
    )
    transaction = SimpleNamespace(
        autenticado=True,
        estado="disponible",
        referencia="1204-1678-0901",
        monto=Decimal("300.00"),
        fecha_correo=now - timedelta(minutes=5),
    )

    assert (
        BankEmailService.transaction_match_reason(
            config,
            revision,
            transaction,
        )
        == "coincidencia_exacta"
    )
    transaction.autenticado = False
    assert (
        BankEmailService.transaction_match_reason(
            config,
            revision,
            transaction,
        )
        == "correo_bancario_no_autenticado"
    )


def test_transaction_rejects_reused_or_out_of_window_email():
    now = datetime(2026, 9, 10, 10, 0)
    config = SimpleNamespace(
        activo=True,
        tolerancia_monto=Decimal("0.00"),
        ventana_dias=3,
    )
    revision = SimpleNamespace(
        folio_detectado="120416780901",
        monto_detectado=Decimal("300.00"),
        fecha_recepcion=now,
    )
    transaction = SimpleNamespace(
        autenticado=True,
        estado="conciliada",
        referencia="120416780901",
        monto=Decimal("300.00"),
        fecha_correo=now,
    )
    assert (
        BankEmailService.transaction_match_reason(
            config,
            revision,
            transaction,
        )
        == "correo_bancario_ya_utilizado"
    )
    transaction.estado = "disponible"
    transaction.fecha_correo = now - timedelta(days=4)
    assert (
        BankEmailService.transaction_match_reason(
            config,
            revision,
            transaction,
        )
        == "fecha_hora_bancaria_fuera_de_ventana"
    )


def test_normalize_bank_sender_accepts_copied_from_header():
    assert (
        _normalize_senders("Banco Azteca <avisos@banco.example>")
        == "avisos@banco.example"
    )
