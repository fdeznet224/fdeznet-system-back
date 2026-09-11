#!/usr/bin/env bash
set -Eeuo pipefail

readonly APP_DIR="/opt/fdeznet"
readonly BACKEND_DIR="$APP_DIR/backend"
readonly FRONTEND_DIR="$APP_DIR/frontend"
readonly BACKEND_REPO="https://github.com/fdeznet224/fdeznet-system-back.git"
readonly FRONTEND_REPO="https://github.com/fdeznet224/fdeznet-system-frontend.git"
readonly CONTROL_URL="https://fdezpay.com/api"
readonly DB_NAME="fdeznet_db"
readonly DB_USER="fdeznet_app"
readonly SERVICE_USER="fdeznet"
readonly MIN_MEMORY_KB=3800000
readonly MIN_DISK_BYTES=30000000000

DOMAIN=""
ADMIN_EMAIL=""
ADMIN_USER="admin"
BOOTSTRAP_TOKEN=""
SKIP_DNS_CHECK="false"
RESET_ADMIN_PASSWORD="false"
ACCESS_HOST=""
PUBLIC_SCHEME="http"
USE_TLS="false"

log() { printf '\n[%s] %s\n' "$(date '+%H:%M:%S')" "$*"; }
fail() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
trap 'fail "La instalación se detuvo inesperadamente en la línea ${LINENO}"' ERR

usage() {
  printf '%s\n' \
    "Uso:" \
    "  sudo bash install.sh --domain isp.ejemplo.com --email admin@ejemplo.com --bootstrap-token TOKEN" \
    "  sudo bash install.sh --bootstrap-token TOKEN  # acceso temporal por IP" \
    "" \
    "Opciones:" \
    "  --admin-user USUARIO     Usuario administrador inicial (default: admin)" \
    "  --skip-dns-check         Omite la validación DNS previa al certificado" \
    "  --reset-admin-password   Genera una nueva contraseña para el administrador"
}

while (($#)); do
  case "$1" in
    --domain) DOMAIN="${2:-}"; shift 2 ;;
    --email) ADMIN_EMAIL="${2:-}"; shift 2 ;;
    --admin-user) ADMIN_USER="${2:-}"; shift 2 ;;
    --bootstrap-token) BOOTSTRAP_TOKEN="${2:-}"; shift 2 ;;
    --skip-dns-check) SKIP_DNS_CHECK="true"; shift ;;
    --reset-admin-password) RESET_ADMIN_PASSWORD="true"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) fail "Opción desconocida: $1" ;;
  esac
done

[[ "${EUID}" -eq 0 ]] || fail "Ejecuta el instalador como root o con sudo"
DOMAIN="${DOMAIN#http://}"
DOMAIN="${DOMAIN#https://}"
DOMAIN="${DOMAIN%%/*}"
if [[ -n "$DOMAIN" ]]; then
  [[ "$DOMAIN" =~ ^([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,63}$ ]] || fail "Dominio inválido"
  [[ "$ADMIN_EMAIL" =~ ^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$ ]] || fail "Correo inválido"
  USE_TLS="true"
  PUBLIC_SCHEME="https"
fi
[[ "$ADMIN_USER" =~ ^[a-zA-Z0-9._-]{3,50}$ ]] || fail "Usuario administrador inválido"

if [[ -r /etc/os-release ]]; then
  . /etc/os-release
  [[ "${ID:-}" == "ubuntu" || "${ID:-}" == "debian" ]] || fail "Solo se admite Ubuntu o Debian"
else
  fail "No se pudo identificar el sistema operativo"
fi

[[ "$(uname -m)" == "x86_64" ]] || fail "Esta versión requiere una VPS x86_64/amd64"
AVAILABLE_MEMORY_KB="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)"
AVAILABLE_DISK_BYTES="$(df --output=avail -B1 / | awk 'NR == 2 {print $1}')"
[[ "$AVAILABLE_MEMORY_KB" -ge "$MIN_MEMORY_KB" ]] || fail "La VPS necesita al menos 4 GB de RAM"
if [[ "$AVAILABLE_DISK_BYTES" -lt "$MIN_DISK_BYTES" ]]; then
  if [[ -d "$BACKEND_DIR/.git" ]]; then
    log "Aviso: quedan menos de 30 GB; libera espacio para conservar los respaldos"
  else
    fail "La VPS necesita al menos 30 GB libres"
  fi
fi

PUBLIC_IP="$(curl -4fsS --max-time 10 https://api.ipify.org)" || fail "No se pudo detectar la IP pública"
ACCESS_HOST="${DOMAIN:-$PUBLIC_IP}"
if [[ "$USE_TLS" == "true" && "$SKIP_DNS_CHECK" != "true" ]]; then
  DNS_IP="$(getent ahostsv4 "$DOMAIN" | awk 'NR == 1 {print $1}')"
  [[ -n "$DNS_IP" ]] || fail "El dominio todavía no tiene un registro DNS A"
  [[ "$DNS_IP" == "$PUBLIC_IP" ]] || fail "El dominio apunta a $DNS_IP, pero esta VPS usa $PUBLIC_IP"
elif [[ "$USE_TLS" != "true" ]]; then
  log "Instalación temporal por IP: se usará http://${PUBLIC_IP} sin certificado TLS"
fi

existing_value() {
  local key="$1"
  local file="$2"
  [[ -f "$file" ]] || return 0
  sed -n "s/^${key}=//p" "$file" | tail -n 1
}

set_env_value() {
  local file="$1"
  local key="$2"
  local value="$3"
  sed -i "/^${key}=/d" "$file"
  printf '%s=%s\n' "$key" "$value" >> "$file"
}

apt_is_busy() {
  local lock
  if command -v fuser >/dev/null 2>&1; then
    for lock in \
      /var/lib/dpkg/lock-frontend \
      /var/lib/dpkg/lock \
      /var/cache/apt/archives/lock \
      /var/lib/apt/lists/lock; do
      if fuser "$lock" >/dev/null 2>&1; then
        return 0
      fi
    done
    return 1
  fi
  pgrep -x apt-get >/dev/null 2>&1 \
    || pgrep -x apt >/dev/null 2>&1 \
    || pgrep -x dpkg >/dev/null 2>&1
}

wait_for_apt() {
  local waited=0
  while apt_is_busy; do
    if ((waited == 0 || waited % 30 == 0)); then
      log "Ubuntu está aplicando actualizaciones; esperando el bloqueo de apt (${waited}s)"
    fi
    ((waited >= 900)) && fail "apt continúa ocupado después de 15 minutos; vuelve a ejecutar el mismo comando"
    sleep 5
    ((waited += 5))
  done
  if ((waited > 0)); then
    log "apt quedó disponible; continuando la instalación"
  fi
}

log "Instalando dependencias del sistema"
export DEBIAN_FRONTEND=noninteractive
wait_for_apt
apt-get update
MYSQL_PACKAGE="mysql-server"
if ! apt-cache show mysql-server >/dev/null 2>&1; then MYSQL_PACKAGE="default-mysql-server"; fi
ASOUND_PACKAGE="libasound2"
if apt-cache show libasound2t64 >/dev/null 2>&1; then ASOUND_PACKAGE="libasound2t64"; fi
apt-get install -y ca-certificates certbot curl git gnupg jq nginx openssl python3-pip python3-venv \
  sudo ufw wireguard "$MYSQL_PACKAGE" build-essential pkg-config libmysqlclient-dev \
  libnss3 libatk-bridge2.0-0 libxcomposite1 libxdamage1 libxrandr2 libgbm1 \
  "$ASOUND_PACKAGE" libpangocairo-1.0-0 libcups2 libxshmfence1 libxss1 \
  fonts-liberation python3-certbot-nginx

if ! command -v node >/dev/null 2>&1 || [[ "$(node --version | tr -d v | cut -d. -f1)" -lt 20 ]]; then
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
  wait_for_apt
  apt-get install -y nodejs
fi

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir /var/lib/fdeznet --shell /usr/sbin/nologin "$SERVICE_USER"
fi
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 "$APP_DIR"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 /var/lib/fdeznet
usermod -aG "$SERVICE_USER" www-data

log "Descargando y preparando el código"
if [[ -d "$BACKEND_DIR/.git" ]]; then
  runuser -u "$SERVICE_USER" -- git -C "$BACKEND_DIR" fetch origin main
  runuser -u "$SERVICE_USER" -- git -C "$BACKEND_DIR" merge --ff-only origin/main
elif [[ -e "$BACKEND_DIR" ]]; then
  fail "$BACKEND_DIR existe pero no es un repositorio Git"
else
  runuser -u "$SERVICE_USER" -- git clone --branch main "$BACKEND_REPO" "$BACKEND_DIR"
fi

if [[ -d "$FRONTEND_DIR/.git" ]]; then
  runuser -u "$SERVICE_USER" -- git -C "$FRONTEND_DIR" fetch origin main
  runuser -u "$SERVICE_USER" -- git -C "$FRONTEND_DIR" merge --ff-only origin/main
elif [[ -e "$FRONTEND_DIR" ]]; then
  fail "$FRONTEND_DIR existe pero no es un repositorio Git"
else
  runuser -u "$SERVICE_USER" -- git clone --branch main "$FRONTEND_REPO" "$FRONTEND_DIR"
fi

NEW_INSTALL="false"
if [[ ! -f "$BACKEND_DIR/.env" ]]; then
  NEW_INSTALL="true"
  install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0600 /dev/null "$BACKEND_DIR/.env"
fi

DB_PASSWORD="$(existing_value DB_PASSWORD "$BACKEND_DIR/.env")"
DB_PASSWORD="${DB_PASSWORD:-$(openssl rand -hex 24)}"
SECRET_KEY="$(existing_value SECRET_KEY "$BACKEND_DIR/.env")"
SECRET_KEY="${SECRET_KEY:-$(openssl rand -hex 32)}"
WEBHOOK_SECRET="$(existing_value WEBHOOK_SECRET "$BACKEND_DIR/.env")"
WEBHOOK_SECRET="${WEBHOOK_SECRET:-$(openssl rand -hex 32)}"
INSTALLATION_ID="$(existing_value FDEZNET_INSTALLATION_ID "$BACKEND_DIR/.env")"
LICENSE_KEY="$(existing_value FDEZNET_LICENSE_KEY "$BACKEND_DIR/.env")"
RELEASE_VERSION="$(existing_value FDEZNET_RELEASE_VERSION "$BACKEND_DIR/.env")"
BACKEND_COMMIT="$(existing_value FDEZNET_BACKEND_COMMIT "$BACKEND_DIR/.env")"
FRONTEND_COMMIT="$(existing_value FDEZNET_FRONTEND_COMMIT "$BACKEND_DIR/.env")"
BRAND_NAME="$(existing_value FDEZNET_BRAND_NAME "$BACKEND_DIR/.env")"
BRAND_SYSTEM_NAME="$(existing_value FDEZNET_BRAND_SYSTEM_NAME "$BACKEND_DIR/.env")"
BRAND_EMAIL="$(existing_value FDEZNET_BRAND_EMAIL "$BACKEND_DIR/.env")"
ADMIN_PASSWORD="$(existing_value ADMIN_BOOTSTRAP_PASSWORD "$BACKEND_DIR/.env")"
if [[ "$RESET_ADMIN_PASSWORD" == "true" ]]; then
  ADMIN_PASSWORD="$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-24)"
  set_env_value "$BACKEND_DIR/.env" ADMIN_BOOTSTRAP_USER "$ADMIN_USER"
  set_env_value "$BACKEND_DIR/.env" ADMIN_BOOTSTRAP_NAME Administrador
  set_env_value "$BACKEND_DIR/.env" ADMIN_BOOTSTRAP_PASSWORD "$ADMIN_PASSWORD"
elif [[ "$NEW_INSTALL" == "true" && -z "$ADMIN_PASSWORD" ]]; then
  ADMIN_PASSWORD="$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-24)"
  set_env_value "$BACKEND_DIR/.env" ADMIN_BOOTSTRAP_USER "$ADMIN_USER"
  set_env_value "$BACKEND_DIR/.env" ADMIN_BOOTSTRAP_NAME Administrador
  set_env_value "$BACKEND_DIR/.env" ADMIN_BOOTSTRAP_PASSWORD "$ADMIN_PASSWORD"
fi

if [[ -z "$INSTALLATION_ID" || -z "$LICENSE_KEY" ]]; then
  [[ -n "$BOOTSTRAP_TOKEN" ]] || fail "Falta --bootstrap-token para activar esta instalación"
  log "Canjeando el token de instalación de un solo uso"
  BOOTSTRAP_RESPONSE="$(curl -fsS --max-time 30 \
    -H 'Content-Type: application/json' \
    -d "$(jq -n --arg token "$BOOTSTRAP_TOKEN" '{token: $token}')" \
    "$CONTROL_URL/control/bootstrap")" || fail "El token fue rechazado o el servidor central no respondió"
  INSTALLATION_ID="$(jq -er '.instalacion_id' <<< "$BOOTSTRAP_RESPONSE")"
  LICENSE_KEY="$(jq -er '.licencia' <<< "$BOOTSTRAP_RESPONSE")"
  RELEASE_VERSION="$(jq -er '.version' <<< "$BOOTSTRAP_RESPONSE")"
  BACKEND_COMMIT="$(jq -er '.backend_commit' <<< "$BOOTSTRAP_RESPONSE")"
  FRONTEND_COMMIT="$(jq -er '.frontend_commit' <<< "$BOOTSTRAP_RESPONSE")"
  BRAND_NAME="$(jq -r '.nombre_isp // empty' <<< "$BOOTSTRAP_RESPONSE")"
  BRAND_SYSTEM_NAME="$BRAND_NAME"
  BRAND_EMAIL="$(jq -r '.contacto_email // empty' <<< "$BOOTSTRAP_RESPONSE")"
  set_env_value "$BACKEND_DIR/.env" FDEZNET_INSTALLATION_ID "$INSTALLATION_ID"
  set_env_value "$BACKEND_DIR/.env" FDEZNET_LICENSE_KEY "$LICENSE_KEY"
  set_env_value "$BACKEND_DIR/.env" FDEZNET_RELEASE_VERSION "$RELEASE_VERSION"
  set_env_value "$BACKEND_DIR/.env" FDEZNET_BACKEND_COMMIT "$BACKEND_COMMIT"
  set_env_value "$BACKEND_DIR/.env" FDEZNET_FRONTEND_COMMIT "$FRONTEND_COMMIT"
fi

[[ "$RELEASE_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "La versión inicial no es válida"
[[ "$BACKEND_COMMIT" =~ ^[0-9a-f]{40}$ ]] || fail "El commit inicial del backend no es válido"
[[ "$FRONTEND_COMMIT" =~ ^[0-9a-f]{40}$ ]] || fail "El commit inicial del frontend no es válido"
log "Fijando la versión ${RELEASE_VERSION} autorizada por el panel central"
runuser -u "$SERVICE_USER" -- git -C "$BACKEND_DIR" fetch origin "$BACKEND_COMMIT"
runuser -u "$SERVICE_USER" -- git -C "$FRONTEND_DIR" fetch origin "$FRONTEND_COMMIT"
runuser -u "$SERVICE_USER" -- git -C "$BACKEND_DIR" reset --hard "$BACKEND_COMMIT"
runuser -u "$SERVICE_USER" -- git -C "$FRONTEND_DIR" reset --hard "$FRONTEND_COMMIT"

log "Configurando MySQL"
systemctl enable --now mysql
mysql --protocol=socket <<SQL
CREATE DATABASE IF NOT EXISTS ${DB_NAME} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS '${DB_USER}'@'127.0.0.1' IDENTIFIED BY '${DB_PASSWORD}';
ALTER USER '${DB_USER}'@'127.0.0.1' IDENTIFIED BY '${DB_PASSWORD}';
GRANT ALL PRIVILEGES ON ${DB_NAME}.* TO '${DB_USER}'@'127.0.0.1';
FLUSH PRIVILEGES;
SQL

set_env_value "$BACKEND_DIR/.env" ENVIRONMENT production
set_env_value "$BACKEND_DIR/.env" DATABASE_URL "mysql+asyncmy://${DB_USER}:${DB_PASSWORD}@127.0.0.1/${DB_NAME}"
set_env_value "$BACKEND_DIR/.env" DB_USER "$DB_USER"
set_env_value "$BACKEND_DIR/.env" DB_PASSWORD "$DB_PASSWORD"
set_env_value "$BACKEND_DIR/.env" DB_HOST 127.0.0.1
set_env_value "$BACKEND_DIR/.env" DB_PORT 3306
set_env_value "$BACKEND_DIR/.env" DB_NAME "$DB_NAME"
set_env_value "$BACKEND_DIR/.env" SECRET_KEY "$SECRET_KEY"
set_env_value "$BACKEND_DIR/.env" WEBHOOK_SECRET "$WEBHOOK_SECRET"
set_env_value "$BACKEND_DIR/.env" CORS_ALLOWED_ORIGINS "${PUBLIC_SCHEME}://${ACCESS_HOST}"
set_env_value "$BACKEND_DIR/.env" PUBLIC_URL "${PUBLIC_SCHEME}://${ACCESS_HOST}"
set_env_value "$BACKEND_DIR/.env" WHATSAPP_BASE_URL http://127.0.0.1:3000
set_env_value "$BACKEND_DIR/.env" VPN_SERVER_IP "$PUBLIC_IP"
set_env_value "$BACKEND_DIR/.env" FDEZNET_CONTROL_PLANE_MODE client
set_env_value "$BACKEND_DIR/.env" FDEZNET_CONTROL_URL "$CONTROL_URL"
set_env_value "$BACKEND_DIR/.env" FDEZNET_INSTALLATION_ID "$INSTALLATION_ID"
set_env_value "$BACKEND_DIR/.env" FDEZNET_LICENSE_KEY "$LICENSE_KEY"
set_env_value "$BACKEND_DIR/.env" FDEZNET_RELEASE_VERSION "$RELEASE_VERSION"
set_env_value "$BACKEND_DIR/.env" FDEZNET_BACKEND_COMMIT "$BACKEND_COMMIT"
set_env_value "$BACKEND_DIR/.env" FDEZNET_FRONTEND_COMMIT "$FRONTEND_COMMIT"
if [[ -n "$BRAND_NAME" ]]; then
  set_env_value "$BACKEND_DIR/.env" FDEZNET_BRAND_NAME "$BRAND_NAME"
  set_env_value "$BACKEND_DIR/.env" FDEZNET_BRAND_SYSTEM_NAME "${BRAND_SYSTEM_NAME:-$BRAND_NAME}"
fi
if [[ -n "$BRAND_EMAIL" ]]; then
  set_env_value "$BACKEND_DIR/.env" FDEZNET_BRAND_EMAIL "$BRAND_EMAIL"
fi
set_env_value "$BACKEND_DIR/.env" FDEZNET_BACKUP_DIR /var/backups/fdeznet
set_env_value "$BACKEND_DIR/.env" FDEZNET_BACKUP_RETENTION_DAYS 14
if [[ -n "$ADMIN_PASSWORD" ]]; then
  set_env_value "$BACKEND_DIR/.env" ADMIN_BOOTSTRAP_USER "$ADMIN_USER"
  set_env_value "$BACKEND_DIR/.env" ADMIN_BOOTSTRAP_NAME Administrador
  set_env_value "$BACKEND_DIR/.env" ADMIN_BOOTSTRAP_PASSWORD "$ADMIN_PASSWORD"
fi
chown "$SERVICE_USER:$SERVICE_USER" "$BACKEND_DIR/.env"
chmod 0600 "$BACKEND_DIR/.env"

log "Configurando WireGuard"
install -d -m 0700 /etc/wireguard
if [[ ! -f /etc/wireguard/server_private.key ]]; then
  (
    umask 077
    wg genkey | tee /etc/wireguard/server_private.key | wg pubkey > /etc/wireguard/server_public.key
  )
fi
if [[ ! -f /etc/wireguard/wg0.conf ]]; then
  PRIVATE_KEY="$(< /etc/wireguard/server_private.key)"
  MAIN_INTERFACE="$(ip route show default | awk 'NR == 1 {print $5}')"
  cat > /etc/wireguard/wg0.conf <<WGCONF
[Interface]
Address = 10.8.0.1/24
ListenPort = 51820
PrivateKey = ${PRIVATE_KEY}
SaveConfig = true
PostUp = iptables -A FORWARD -i wg0 -j ACCEPT; iptables -t nat -A POSTROUTING -o ${MAIN_INTERFACE} -j MASQUERADE
PostDown = iptables -D FORWARD -i wg0 -j ACCEPT; iptables -t nat -D POSTROUTING -o ${MAIN_INTERFACE} -j MASQUERADE
WGCONF
  chmod 0600 /etc/wireguard/wg0.conf
fi
printf '%s\n' 'net.ipv4.ip_forward=1' > /etc/sysctl.d/99-fdeznet.conf
sysctl --system >/dev/null
printf '%s\n' "${SERVICE_USER} ALL=(root) NOPASSWD: /usr/bin/wg, /usr/bin/wg-quick" > /etc/sudoers.d/fdeznet-vpn
chmod 0440 /etc/sudoers.d/fdeznet-vpn
visudo -cf /etc/sudoers.d/fdeznet-vpn >/dev/null
systemctl enable --now wg-quick@wg0

log "Instalando backend y aplicando migraciones"
if [[ ! -x "$BACKEND_DIR/venv/bin/python" ]]; then
  runuser -u "$SERVICE_USER" -- python3 -m venv "$BACKEND_DIR/venv"
fi
runuser -u "$SERVICE_USER" -- "$BACKEND_DIR/venv/bin/pip" install --upgrade pip
# Una instalación reanudada puede conservar un release anterior que todavía no
# declaraba cryptography. Instalarla aquí permite autenticar MySQL antes de que
# el mecanismo normal de actualizaciones lleve el repositorio al release nuevo.
runuser -u "$SERVICE_USER" -- "$BACKEND_DIR/venv/bin/pip" install cryptography==50.0.1
runuser -u "$SERVICE_USER" -- "$BACKEND_DIR/venv/bin/pip" install -r "$BACKEND_DIR/requirements.txt"
DATABASE_STATE="$(
  cd "$BACKEND_DIR"
  runuser -u "$SERVICE_USER" -- ./venv/bin/python - <<'PY'
import asyncio

from sqlalchemy import inspect, text

from src.infrastructure import models  # noqa: F401
from src.infrastructure.database import Base, engine


async def prepare_database() -> None:
    async with engine.begin() as connection:
        tables = await connection.run_sync(
            lambda sync_connection: set(inspect(sync_connection).get_table_names())
        )
        application_tables = tables - {"alembic_version"}

        if application_tables:
            if "clientes" not in application_tables:
                raise RuntimeError(
                    "La base contiene tablas parciales pero no la tabla clientes; "
                    "se requiere revisión antes de continuar"
                )
            print("existing")
            return

        # La primera revisión de Alembic es una línea base histórica y supone
        # que estas tablas ya existen. En una VPS nueva se crea el esquema del
        # release fijado y después Alembic lo registra en la revisión actual.
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(
            text(
                """
                INSERT INTO politicas_cobranza (
                    nombre, tipo_cliente, dias_max_promesa,
                    max_promesas_activas, max_incumplidas_90_dias,
                    permite_reconexion, activa
                )
                SELECT 'Residencial', 'residencial', 7, 1, 2, 1, 1
                WHERE NOT EXISTS (
                    SELECT 1 FROM politicas_cobranza
                    WHERE tipo_cliente = 'residencial'
                )
                """
            )
        )
        print("fresh")


asyncio.run(prepare_database())
PY
)"

if [[ "$DATABASE_STATE" == "fresh" ]]; then
  log "Base nueva detectada; registrando el esquema inicial en Alembic"
  (cd "$BACKEND_DIR" && runuser -u "$SERVICE_USER" -- ./venv/bin/alembic stamp head)
elif [[ "$DATABASE_STATE" == "existing" ]]; then
  (cd "$BACKEND_DIR" && runuser -u "$SERVICE_USER" -- ./venv/bin/alembic upgrade head)
else
  fail "No se pudo determinar el estado de la base de datos"
fi
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 "$BACKEND_DIR/static/recibos"

if [[ "$RESET_ADMIN_PASSWORD" == "true" ]]; then
  log "Restableciendo la contraseña del administrador inicial"
  (
    cd "$BACKEND_DIR"
    runuser -u "$SERVICE_USER" -- ./venv/bin/python - <<'PY'
import asyncio
import os

from sqlalchemy import select

from src.application.services.user_service import UserService
from src.infrastructure.database import SessionLocal
from src.infrastructure.models import UsuarioModel


async def reset_admin_password() -> None:
    async with SessionLocal() as database:
        admin = await database.scalar(
            select(UsuarioModel)
            .where(UsuarioModel.rol == "admin")
            .order_by(UsuarioModel.id)
            .limit(1)
        )
        if admin is None:
            print("pending-bootstrap")
            return
        admin.password_hash = UserService(database).get_password_hash(
            os.environ["ADMIN_BOOTSTRAP_PASSWORD"]
        )
        admin.activo = True
        await database.commit()
        print("reset")


asyncio.run(reset_admin_password())
PY
  )
fi

log "Configurando respaldos y actualizaciones seguras"
install -d -m 0700 /etc/fdeznet
install -d -o root -g "${SERVICE_USER}" -m 0750 /var/backups/fdeznet
if [[ ! -f /etc/fdeznet/backup.key ]]; then
  (
    umask 077
    openssl rand -hex 32 > /etc/fdeznet/backup.key
  )
fi
chmod 0600 /etc/fdeznet/backup.key
install -o root -g root -m 0750 "$BACKEND_DIR/scripts/fdeznet-maintenance.sh" /usr/local/sbin/fdeznet-maintenance
install -o root -g root -m 0644 "$BACKEND_DIR/deploy/systemd/fdeznet-backup.service" /etc/systemd/system/fdeznet-backup.service
install -o root -g root -m 0644 "$BACKEND_DIR/deploy/systemd/fdeznet-backup.timer" /etc/systemd/system/fdeznet-backup.timer
install -o root -g root -m 0644 "$BACKEND_DIR/deploy/systemd/fdeznet-update.service" /etc/systemd/system/fdeznet-update.service
install -o root -g root -m 0644 "$BACKEND_DIR/deploy/systemd/fdeznet-update.timer" /etc/systemd/system/fdeznet-update.timer
install -o root -g root -m 0644 "$BACKEND_DIR/deploy/systemd/fdeznet-verify.service" /etc/systemd/system/fdeznet-verify.service
install -o root -g root -m 0644 "$BACKEND_DIR/deploy/systemd/fdeznet-verify.timer" /etc/systemd/system/fdeznet-verify.timer
printf '%s\n' \
  "${SERVICE_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl start --no-block fdeznet-backup.service, /usr/bin/systemctl start --no-block fdeznet-update.service, /usr/bin/systemctl start --no-block fdeznet-verify.service" \
  > /etc/sudoers.d/fdeznet-maintenance
chmod 0440 /etc/sudoers.d/fdeznet-maintenance
visudo -cf /etc/sudoers.d/fdeznet-maintenance >/dev/null

cat > /etc/systemd/system/fdeznet-api.service <<UNIT
[Unit]
Description=FdezNet API
After=network-online.target mysql.service
Wants=network-online.target
[Service]
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${BACKEND_DIR}
EnvironmentFile=${BACKEND_DIR}/.env
ExecStart=${BACKEND_DIR}/venv/bin/uvicorn src.main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5
PrivateTmp=true
[Install]
WantedBy=multi-user.target
UNIT

log "Instalando servicio de WhatsApp"
runuser -u "$SERVICE_USER" -- npm --prefix "$BACKEND_DIR/bot_whatsapp" ci --omit=dev
cat > "$BACKEND_DIR/bot_whatsapp/.env" <<BOTENV
PORT=3000
PUBLIC_URL=${PUBLIC_SCHEME}://${ACCESS_HOST}/media
API_BACKEND_URL=http://127.0.0.1:8000
WEBHOOK_SECRET=${WEBHOOK_SECRET}
BOTENV
chown "$SERVICE_USER:$SERVICE_USER" "$BACKEND_DIR/bot_whatsapp/.env"
chmod 0600 "$BACKEND_DIR/bot_whatsapp/.env"
cat > /etc/systemd/system/fdeznet-bot.service <<UNIT
[Unit]
Description=FdezNet WhatsApp Bot
After=network-online.target fdeznet-api.service
[Service]
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${BACKEND_DIR}/bot_whatsapp
Environment=NODE_ENV=production
EnvironmentFile=${BACKEND_DIR}/bot_whatsapp/.env
ExecStart=/usr/bin/node index.js
Restart=always
RestartSec=5
PrivateTmp=true
[Install]
WantedBy=multi-user.target
UNIT

log "Compilando frontend"
runuser -u "$SERVICE_USER" -- npm --prefix "$FRONTEND_DIR" ci
runuser -u "$SERVICE_USER" -- npm --prefix "$FRONTEND_DIR" run build
# Los recursos del frontend son públicos. Nginx necesita atravesar la ruta y
# leerlos aunque los secretos del backend permanezcan con permisos 0600.
chmod 0755 "$APP_DIR" "$FRONTEND_DIR"
find "$FRONTEND_DIR/dist" -type d -exec chmod 0755 {} +
find "$FRONTEND_DIR/dist" -type f -exec chmod 0644 {} +

log "Configurando Nginx"
cat > /etc/nginx/sites-available/fdeznet <<NGINX
server {
    listen 80;
    listen [::]:80;
    server_name ${ACCESS_HOST};
    client_max_body_size 12m;
    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
    location /media/uploads/ {
        proxy_pass http://127.0.0.1:3000/uploads/;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
    location / {
        root ${FRONTEND_DIR}/dist;
        try_files \$uri \$uri/ /index.html;
    }
}
NGINX
ln -sfn /etc/nginx/sites-available/fdeznet /etc/nginx/sites-enabled/fdeznet
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl daemon-reload
systemctl enable --now fdeznet-api fdeznet-bot nginx
systemctl enable --now fdeznet-backup.timer fdeznet-update.timer fdeznet-verify.timer
systemctl restart fdeznet-api fdeznet-bot nginx

for attempt in {1..150}; do
  if curl -fsS http://127.0.0.1:8000/health/ready >/dev/null 2>&1; then break; fi
  if ((attempt == 1 || attempt % 15 == 0)); then
    log "Esperando el primer arranque de la API (${attempt}/150)"
  fi
  [[ "$attempt" -lt 150 ]] || fail "La API no quedó lista después de 5 minutos; revisa journalctl -u fdeznet-api"
  sleep 2
done

if [[ "$USE_TLS" == "true" ]]; then
  log "Configurando certificado HTTPS"
  certbot --nginx --non-interactive --agree-tos --redirect -m "$ADMIN_EMAIL" -d "$DOMAIN"
fi
SSH_PORT="$(sshd -T 2>/dev/null | awk '/^port / {print $2; exit}' || true)"
SSH_PORT="${SSH_PORT:-22}"
ufw allow "${SSH_PORT}/tcp"
ufw allow 'Nginx Full'
ufw allow 51820/udp
ufw --force enable

if [[ -n "$ADMIN_PASSWORD" ]]; then
  sed -i '/^ADMIN_BOOTSTRAP_PASSWORD=/d' "$BACKEND_DIR/.env"
  systemctl restart fdeznet-api
  for attempt in {1..150}; do
    if curl -fsS http://127.0.0.1:8000/health/ready >/dev/null 2>&1; then break; fi
    if ((attempt == 1 || attempt % 15 == 0)); then
      log "Esperando el reinicio final de la API (${attempt}/150)"
    fi
    [[ "$attempt" -lt 150 ]] || fail "La API no volvió a quedar lista después de 5 minutos; revisa journalctl -u fdeznet-api"
    sleep 2
  done
fi

curl -fsS "${PUBLIC_SCHEME}://${ACCESS_HOST}/api/health/ready" >/dev/null
log "Instalación completada"
printf '%s\n' \
  "Panel: ${PUBLIC_SCHEME}://${ACCESS_HOST}" \
  "Usuario inicial: ${ADMIN_USER}" \
  "Contraseña inicial: ${ADMIN_PASSWORD:-la configurada anteriormente}" \
  "ID de instalación: ${INSTALLATION_ID}" \
  "La VPS reportará su versión a fdezpay.com automáticamente."
