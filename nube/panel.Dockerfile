# Panel central del dueño: maneja los contenedores de cada ISP por el socket de Docker.
FROM docker:27-cli AS docker
FROM rclone/rclone:1.68.2 AS rclone

FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONPATH=/opt/panel TZ=America/Mexico_City
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssl tzdata curl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=docker /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose
# Copia de los respaldos fuera de la VPS (Cloudflare R2, Backblaze B2, Drive…).
COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone
RUN pip install fastapi==0.135.2 starlette==1.3.1 "uvicorn[standard]==0.47.0" Jinja2==3.1.6 \
        python-multipart==0.0.32 SQLAlchemy==2.0.48 PyJWT==2.14.0 passlib==1.7.4 httpx==0.28.1
WORKDIR /opt/panel
COPY nube/__init__.py nube/__init__.py
COPY nube/panel nube/panel
EXPOSE 8100
HEALTHCHECK --interval=30s --timeout=5s CMD curl -fsS http://127.0.0.1:8100/salud || exit 1
CMD ["uvicorn", "nube.panel.app:app", "--host", "0.0.0.0", "--port", "8100", "--proxy-headers", "--forwarded-allow-ips=*"]
