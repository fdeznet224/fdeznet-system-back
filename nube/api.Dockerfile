# API de una instancia de la nube: FastAPI + jobs + WireGuard propio del ISP.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        wireguard-tools iproute2 iptables snmp curl ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# torch en su versión CPU: la de PyPI trae CUDA y pesa varios GB más.
COPY requirements.txt .
RUN pip install torch==2.13.0 torchvision==0.28.0 --index-url https://download.pytorch.org/whl/cpu \
    && pip install -r requirements.txt

COPY alembic.ini .
COPY migrations ./migrations
COPY src ./src
COPY nube/entrypoint-api.sh /usr/local/bin/entrypoint-api.sh
COPY nube/preparar_base.py ./nube/preparar_base.py
RUN chmod 0755 /usr/local/bin/entrypoint-api.sh \
    && mkdir -p static/recibos static/branding uploads/ordenes bot_whatsapp/uploads /var/backups/fdeznet \
    && chown 1000:1000 bot_whatsapp/uploads
# bot_whatsapp/uploads se comparte con el bot (usuario node, uid 1000).

# Dentro del contenedor no hay sudo: WireGuard se maneja con NET_ADMIN.
ENV SUDO_BIN= \
    WG_BIN=/usr/bin/wg \
    WG_QUICK_BIN=/usr/bin/wg-quick \
    WG_INTERFACE=wg0

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s \
    CMD curl -fsS http://127.0.0.1:8000/health/ready || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint-api.sh"]
