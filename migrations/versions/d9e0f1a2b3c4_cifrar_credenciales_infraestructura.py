"""cifra credenciales de routers y OLT

Revision ID: d9e0f1a2b3c4
Revises: c8d9e0f1a2b3
"""

import base64
import hashlib
import os

from alembic import op
from cryptography.fernet import Fernet
import sqlalchemy as sa


revision = "d9e0f1a2b3c4"
down_revision = "c8d9e0f1a2b3"
branch_labels = None
depends_on = None
PREFIX = "enc:v1:"


def _cipher() -> Fernet:
    secret = os.getenv("SECRET_KEY", "").strip()
    if not secret:
        raise RuntimeError("SECRET_KEY es obligatoria para migrar credenciales")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


def _encrypt_existing(table: str, column: str) -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(f"SELECT id, {column} AS secret_value FROM {table} WHERE {column} IS NOT NULL")
    ).mappings()
    cipher = _cipher()
    for row in rows:
        value = str(row["secret_value"])
        if value.startswith(PREFIX):
            continue
        encrypted = PREFIX + cipher.encrypt(value.encode()).decode()
        connection.execute(
            sa.text(f"UPDATE {table} SET {column} = :value WHERE id = :identifier"),
            {"value": encrypted, "identifier": row["id"]},
        )


def _decrypt_existing(table: str, column: str) -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(f"SELECT id, {column} AS secret_value FROM {table} WHERE {column} IS NOT NULL")
    ).mappings()
    cipher = _cipher()
    for row in rows:
        value = str(row["secret_value"])
        if not value.startswith(PREFIX):
            continue
        plain = cipher.decrypt(value[len(PREFIX):].encode()).decode()
        connection.execute(
            sa.text(f"UPDATE {table} SET {column} = :value WHERE id = :identifier"),
            {"value": plain, "identifier": row["id"]},
        )


def upgrade() -> None:
    op.alter_column("routers", "pass_api", existing_type=sa.String(100), type_=sa.Text(), nullable=False)
    op.alter_column("olts", "api_password", existing_type=sa.String(255), type_=sa.Text(), nullable=True)
    _encrypt_existing("routers", "pass_api")
    _encrypt_existing("olts", "api_password")


def downgrade() -> None:
    _decrypt_existing("routers", "pass_api")
    _decrypt_existing("olts", "api_password")
    op.alter_column("routers", "pass_api", existing_type=sa.Text(), type_=sa.String(100), nullable=False)
    op.alter_column("olts", "api_password", existing_type=sa.Text(), type_=sa.String(255), nullable=True)
