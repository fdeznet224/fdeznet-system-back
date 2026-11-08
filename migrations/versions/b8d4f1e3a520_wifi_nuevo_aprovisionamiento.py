"""aprovisionamientos_onu.wifi_nuevo: avisar al cliente su WiFi nuevo tras un cambio de ONU

Revision ID: b8d4f1e3a520
Revises: a7c3e9d2f410
Create Date: 2026-11-08
"""

import sqlalchemy as sa
from alembic import op

revision = "b8d4f1e3a520"
down_revision = "a7c3e9d2f410"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("aprovisionamientos_onu",
                  sa.Column("wifi_nuevo", sa.Boolean(), nullable=False, server_default="0"))


def downgrade():
    op.drop_column("aprovisionamientos_onu", "wifi_nuevo")
