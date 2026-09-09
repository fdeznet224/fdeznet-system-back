#!/usr/bin/env bash
set -Eeuo pipefail

readonly APP_DIR="/opt/fdeznet"
readonly BACKEND_DIR="$APP_DIR/backend"
readonly FRONTEND_DIR="$APP_DIR/frontend"
readonly ENV_FILE="$BACKEND_DIR/.env"
readonly KEY_FILE="/etc/fdeznet/backup.key"
readonly STATUS_FILE="/var/lib/fdeznet/maintenance-status.json"
readonly LOCK_FILE="/run/lock/fdeznet-maintenance.lock"
readonly MANUAL_UPDATE_REQUEST_FILE="/var/lib/fdeznet/manual-update-requested"

BACKUP_DIR="/var/backups/fdeznet"
RETENTION_DAYS="14"
REMOTE_DIR=""
LAST_BACKUP=""
OLD_BACKEND=""
OLD_FRONTEND=""
TARGET_VERSION=""
UPDATE_ACTIVE="false"
DB_USER_VALUE=""
DB_PASSWORD_VALUE=""
DB_HOST_VALUE=""
DB_PORT_VALUE=""
DB_NAME_VALUE=""
CURRENT_STAGE=""
CURRENT_ARCHIVE=""
RECOVERY_STATUS="sin_revision"
RECOVERY_DATE=""

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
fail() { printf 'ERROR: %s\n' "$*" >&2; return 1; }

env_value() {
  local key="$1"
  sed -n "s/^${key}=//p" "$ENV_FILE" | tail -n 1
}

write_status() {
  local state="$1"
  local message="$2"
  install -d -m 0755 /var/lib/fdeznet
  jq -n \
    --arg estado "$state" \
    --arg mensaje "$message" \
    --arg fecha "$(date --iso-8601=seconds)" \
    --arg version "$TARGET_VERSION" \
    --arg respaldo "$LAST_BACKUP" \
    --arg recuperacion_estado "$RECOVERY_STATUS" \
    --arg recuperacion_fecha "$RECOVERY_DATE" \
    --arg clave_huella "$(sha256sum "$KEY_FILE" | awk '{print $1}')" \
    '{estado:$estado,mensaje:$mensaje,fecha:$fecha,version:$version,respaldo:$respaldo,
      recuperacion_estado:$recuperacion_estado,recuperacion_fecha:$recuperacion_fecha,
      clave_huella:$clave_huella}' \
    > "${STATUS_FILE}.tmp"
  chmod 0644 "${STATUS_FILE}.tmp"
  mv "${STATUS_FILE}.tmp" "$STATUS_FILE"
}

report() {
  local state="$1"
  local message="$2"
  local installation_id license_key control_url
  installation_id="$(env_value FDEZNET_INSTALLATION_ID)"
  license_key="$(env_value FDEZNET_LICENSE_KEY)"
  control_url="$(env_value FDEZNET_CONTROL_URL)"
  [[ -n "$installation_id" && -n "$license_key" && -n "$control_url" ]] || return 0
  curl -fsS --max-time 20 \
    -H 'Content-Type: application/json' \
    -H "X-Installation-ID: ${installation_id}" \
    -H "X-License-Key: ${license_key}" \
    -d "$(jq -n --arg estado "$state" --arg version "$TARGET_VERSION" --arg mensaje "$message" --arg respaldo "${LAST_BACKUP##*/}" '{estado:$estado,version:$version,mensaje:$mensaje,respaldo:$respaldo}')" \
    "${control_url%/}/control/update-report" >/dev/null || true
}

load_db_config() {
  local database_url
  local -a parsed=()
  DB_USER_VALUE="$(env_value DB_USER)"
  DB_PASSWORD_VALUE="$(env_value DB_PASSWORD)"
  DB_HOST_VALUE="$(env_value DB_HOST)"
  DB_PORT_VALUE="$(env_value DB_PORT)"
  DB_NAME_VALUE="$(env_value DB_NAME)"
  database_url="$(env_value DATABASE_URL)"
  if [[ -n "$database_url" && ( -z "$DB_USER_VALUE" || -z "$DB_NAME_VALUE" ) ]]; then
    mapfile -d '' -t parsed < <(
      DATABASE_URL_VALUE="$database_url" "$BACKEND_DIR/venv/bin/python" -c '
import os
from sqlalchemy.engine import make_url
url = make_url(os.environ["DATABASE_URL_VALUE"])
for value in (url.username, url.password, url.host, url.port or 3306, url.database):
    print("" if value is None else value, end="\0")
'
    )
    DB_USER_VALUE="${parsed[0]:-}"
    DB_PASSWORD_VALUE="${parsed[1]:-}"
    DB_HOST_VALUE="${parsed[2]:-}"
    DB_PORT_VALUE="${parsed[3]:-}"
    DB_NAME_VALUE="${parsed[4]:-}"
  fi
  DB_HOST_VALUE="${DB_HOST_VALUE:-127.0.0.1}"
  DB_PORT_VALUE="${DB_PORT_VALUE:-3306}"
  [[ -n "$DB_USER_VALUE" && -n "$DB_NAME_VALUE" ]] || fail "Configuración de base de datos incompleta"
}

mysql_escape() {
  local value="$1"
  value="${value//\\/\\\\}"
  printf '%s' "${value//\"/\\\"}"
}

mysql_defaults() {
  local target="$1"
  load_db_config
  cat > "$target" <<MYSQL
[client]
user="$(mysql_escape "$DB_USER_VALUE")"
password="$(mysql_escape "$DB_PASSWORD_VALUE")"
host="$(mysql_escape "$DB_HOST_VALUE")"
port=${DB_PORT_VALUE}
MYSQL
  chmod 0600 "$target"
}

verify_backup() {
  local encrypted="${1:-}" metadata
  if [[ -z "$encrypted" ]]; then
    encrypted="$(find "$BACKUP_DIR" -maxdepth 1 -type f -name '*.tar.gz.gpg' -printf '%T@ %p\n' | sort -nr | awk 'NR == 1 {print $2}')"
  fi
  [[ "$encrypted" == "$BACKUP_DIR/"*.tar.gz.gpg && -f "$encrypted" ]] || fail "No hay respaldo para verificar"
  sha256sum -c "${encrypted}.sha256" >/dev/null
  gpg --batch --quiet --decrypt --pinentry-mode loopback \
    --passphrase-file "$KEY_FILE" "$encrypted" | tar -tzf - | \
    awk '$0 == "./data/database.sql.gz" {found=1} END {exit !found}'
  gpg --batch --quiet --decrypt --pinentry-mode loopback \
    --passphrase-file "$KEY_FILE" "$encrypted" | \
    tar -xOzf - ./data/database.sql.gz | gzip -t
  metadata="$(gpg --batch --quiet --decrypt --pinentry-mode loopback \
    --passphrase-file "$KEY_FILE" "$encrypted" | tar -xOzf - ./metadata.env)"
  grep -Eq '^BACKEND_COMMIT=[0-9a-f]{40}$' <<< "$metadata"
  grep -Eq '^FRONTEND_COMMIT=[0-9a-f]{40}$' <<< "$metadata"
  LAST_BACKUP="$encrypted"
  RECOVERY_STATUS="verificado"
  RECOVERY_DATE="$(date --iso-8601=seconds)"
  write_status "recuperacion_verificada" "El respaldo puede descifrarse y su base de datos está íntegra"
  log "Prueba de recuperación aprobada: $encrypted"
}

create_backup() {
  local timestamp stage archive encrypted mysql_config bot_was_active
  timestamp="$(date '+%Y%m%d-%H%M%S')"
  install -d -m 0700 "$BACKUP_DIR"
  stage="$(mktemp -d "$BACKUP_DIR/.stage-${timestamp}.XXXXXX")"
  archive="$BACKUP_DIR/.${timestamp}.tar.gz"
  CURRENT_STAGE="$stage"
  CURRENT_ARCHIVE="$archive"
  encrypted="$BACKUP_DIR/${timestamp}.tar.gz.gpg"
  mysql_config="$stage/mysql.cnf"
  mysql_defaults "$mysql_config"
  bot_was_active="$(systemctl is-active fdeznet-bot 2>/dev/null || true)"
  if [[ "$bot_was_active" == "active" ]]; then systemctl stop fdeznet-bot; fi

  mkdir -p "$stage/data" "$stage/config"
  mysqldump --defaults-extra-file="$mysql_config" --single-transaction \
    --routines --events --triggers --databases "$DB_NAME_VALUE" | gzip -9 > "$stage/data/database.sql.gz"
  cp -a "$ENV_FILE" "$stage/config/backend.env"
  [[ -f "$BACKEND_DIR/bot_whatsapp/.env" ]] && cp -a "$BACKEND_DIR/bot_whatsapp/.env" "$stage/config/bot.env"
  [[ -f /etc/nginx/sites-available/fdeznet ]] && cp -a /etc/nginx/sites-available/fdeznet "$stage/config/nginx.conf"
  [[ -d /etc/wireguard ]] && cp -a /etc/wireguard "$stage/config/wireguard"
  [[ -d "$BACKEND_DIR/static" ]] && cp -a "$BACKEND_DIR/static" "$stage/data/static"
  [[ -d "$BACKEND_DIR/bot_whatsapp/.wwebjs_auth" ]] && cp -a "$BACKEND_DIR/bot_whatsapp/.wwebjs_auth" "$stage/data/whatsapp-auth"
  [[ -d "$BACKEND_DIR/bot_whatsapp/uploads" ]] && cp -a "$BACKEND_DIR/bot_whatsapp/uploads" "$stage/data/whatsapp-uploads"
  printf '%s\n' \
    "BACKEND_COMMIT=$(git -C "$BACKEND_DIR" rev-parse HEAD)" \
    "FRONTEND_COMMIT=$(git -C "$FRONTEND_DIR" rev-parse HEAD)" \
    "CREATED_AT=$(date --iso-8601=seconds)" > "$stage/metadata.env"
  rm -f "$mysql_config"
  if [[ "$bot_was_active" == "active" ]]; then systemctl start fdeznet-bot; fi

  tar -C "$stage" -czf "$archive" .
  gpg --batch --yes --symmetric --cipher-algo AES256 \
    --pinentry-mode loopback --passphrase-file "$KEY_FILE" \
    --output "$encrypted" "$archive"
  gpg --batch --quiet --decrypt --pinentry-mode loopback \
    --passphrase-file "$KEY_FILE" "$encrypted" | tar -tzf - >/dev/null
  sha256sum "$encrypted" > "${encrypted}.sha256"
  chmod 0600 "$encrypted" "${encrypted}.sha256"
  shred -u "$archive"
  rm -rf "$stage"
  CURRENT_STAGE=""
  CURRENT_ARCHIVE=""
  LAST_BACKUP="$encrypted"

  if [[ -n "$REMOTE_DIR" ]]; then
    [[ -d "$REMOTE_DIR" && -w "$REMOTE_DIR" ]] || return 1
    install -m 0600 "$encrypted" "$REMOTE_DIR/"
    install -m 0600 "${encrypted}.sha256" "$REMOTE_DIR/"
  fi
  find "$BACKUP_DIR" -maxdepth 1 -type f -name '*.tar.gz.gpg' -mtime "+$RETENTION_DAYS" -delete
  find "$BACKUP_DIR" -maxdepth 1 -type f -name '*.tar.gz.gpg.sha256' -mtime "+$RETENTION_DAYS" -delete
  write_status "respaldado" "Respaldo cifrado y verificado"
  report "respaldado" "Respaldo cifrado y verificado"
  log "Respaldo verificado: $encrypted"
}

wait_for_health() {
  local public_url attempt
  public_url="$(env_value PUBLIC_URL)"
  for attempt in {1..45}; do
    if curl -fsS --max-time 5 http://127.0.0.1:8000/health/ready >/dev/null; then
      if [[ -z "$public_url" ]] || curl -fsS --max-time 8 "${public_url%/}/api/health/ready" >/dev/null; then
        return 0
      fi
    fi
    sleep 2
  done
  return 1
}

restore_backup() {
  local encrypted="$1"
  local restore_dir mysql_config backend_commit frontend_commit
  [[ "$encrypted" == "$BACKUP_DIR/"*.tar.gz.gpg && -f "$encrypted" ]] || return 1
  sha256sum -c "${encrypted}.sha256" >/dev/null
  restore_dir="$(mktemp -d "$BACKUP_DIR/.restore.XXXXXX")"
  gpg --batch --quiet --decrypt --pinentry-mode loopback \
    --passphrase-file "$KEY_FILE" --output "$restore_dir/archive.tar.gz" "$encrypted"
  tar -C "$restore_dir" -xzf "$restore_dir/archive.tar.gz"
  backend_commit="$(sed -n 's/^BACKEND_COMMIT=//p' "$restore_dir/metadata.env")"
  frontend_commit="$(sed -n 's/^FRONTEND_COMMIT=//p' "$restore_dir/metadata.env")"
  [[ "$backend_commit" =~ ^[0-9a-f]{40}$ && "$frontend_commit" =~ ^[0-9a-f]{40}$ ]] || return 1

  systemctl stop fdeznet-api fdeznet-bot || true
  git -C "$BACKEND_DIR" reset --hard "$backend_commit"
  git -C "$FRONTEND_DIR" reset --hard "$frontend_commit"
  install -o root -g root -m 0750 "$BACKEND_DIR/scripts/fdeznet-maintenance.sh" /usr/local/sbin/fdeznet-maintenance
  cp -a "$restore_dir/config/backend.env" "$ENV_FILE"
  [[ -f "$restore_dir/config/bot.env" ]] && cp -a "$restore_dir/config/bot.env" "$BACKEND_DIR/bot_whatsapp/.env"
  mysql_config="$restore_dir/mysql.cnf"
  mysql_defaults "$mysql_config"
  gzip -dc "$restore_dir/data/database.sql.gz" | mysql --defaults-extra-file="$mysql_config"
  [[ -d "$restore_dir/data/static" ]] && { rm -rf "$BACKEND_DIR/static"; cp -a "$restore_dir/data/static" "$BACKEND_DIR/static"; }
  [[ -d "$restore_dir/data/whatsapp-auth" ]] && { rm -rf "$BACKEND_DIR/bot_whatsapp/.wwebjs_auth"; cp -a "$restore_dir/data/whatsapp-auth" "$BACKEND_DIR/bot_whatsapp/.wwebjs_auth"; }
  [[ -d "$restore_dir/data/whatsapp-uploads" ]] && { rm -rf "$BACKEND_DIR/bot_whatsapp/uploads"; cp -a "$restore_dir/data/whatsapp-uploads" "$BACKEND_DIR/bot_whatsapp/uploads"; }
  chown -R fdeznet:fdeznet "$BACKEND_DIR" "$FRONTEND_DIR"
  runuser -u fdeznet -- "$BACKEND_DIR/venv/bin/pip" install -r "$BACKEND_DIR/requirements.txt"
  runuser -u fdeznet -- npm --prefix "$BACKEND_DIR/bot_whatsapp" ci --omit=dev
  runuser -u fdeznet -- npm --prefix "$FRONTEND_DIR" ci
  runuser -u fdeznet -- npm --prefix "$FRONTEND_DIR" run build
  systemctl restart fdeznet-api fdeznet-bot nginx
  rm -rf "$restore_dir"
  wait_for_health
}

on_error() {
  local code=$?
  trap - ERR
  if [[ -n "$CURRENT_ARCHIVE" && "$CURRENT_ARCHIVE" == "$BACKUP_DIR/."*.tar.gz ]]; then
    shred -u "$CURRENT_ARCHIVE" 2>/dev/null || true
  fi
  if [[ -n "$CURRENT_STAGE" && "$CURRENT_STAGE" == "$BACKUP_DIR/.stage-"* ]]; then
    rm -rf "$CURRENT_STAGE"
  fi
  if [[ "$UPDATE_ACTIVE" == "true" && -n "$LAST_BACKUP" ]]; then
    log "La actualización falló; iniciando reversión automática"
    if restore_backup "$LAST_BACKUP"; then
      TARGET_VERSION=""
      write_status "revertida" "Actualización fallida; respaldo restaurado"
      report "revertida" "Actualización fallida; respaldo restaurado"
    else
      write_status "fallida" "Falló la actualización y también la reversión"
      report "fallida" "Se requiere intervención manual"
    fi
  else
    systemctl start fdeznet-bot >/dev/null 2>&1 || true
    write_status "fallida" "La operación de mantenimiento falló"
    report "fallida" "La operación de mantenimiento falló antes de aplicar cambios"
  fi
  exit "$code"
}
trap on_error ERR

perform_update() {
  local installation_id license_key control_url manifest available backend_commit frontend_commit signature canonical expected
  local -a request_headers=()
  installation_id="$(env_value FDEZNET_INSTALLATION_ID)"
  license_key="$(env_value FDEZNET_LICENSE_KEY)"
  control_url="$(env_value FDEZNET_CONTROL_URL)"
  [[ -n "$installation_id" && -n "$license_key" && -n "$control_url" ]] || return 0
  if [[ -f "$MANUAL_UPDATE_REQUEST_FILE" ]]; then
    request_headers=(-H 'X-Update-Requested: true')
  fi
  manifest="$(curl -fsS --max-time 30 \
    -H "X-Installation-ID: ${installation_id}" \
    -H "X-License-Key: ${license_key}" \
    "${request_headers[@]}" \
    "${control_url%/}/control/update-manifest")"
  rm -f "$MANUAL_UPDATE_REQUEST_FILE"
  available="$(jq -r '.actualizacion_disponible' <<< "$manifest")"
  [[ "$available" == "true" ]] || { write_status "sin_cambios" "No hay actualización autorizada"; return 0; }
  TARGET_VERSION="$(jq -er '.version' <<< "$manifest")"
  backend_commit="$(jq -er '.backend_commit' <<< "$manifest")"
  frontend_commit="$(jq -er '.frontend_commit' <<< "$manifest")"
  signature="$(jq -er '.firma' <<< "$manifest")"
  [[ "$TARGET_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1
  [[ "$backend_commit" =~ ^[0-9a-f]{40}$ && "$frontend_commit" =~ ^[0-9a-f]{40}$ ]] || return 1
  [[ -z "$(git -C "$BACKEND_DIR" status --porcelain --untracked-files=no -- . ':(exclude)bot_whatsapp/node_modules')" ]] || return 1
  [[ -z "$(git -C "$FRONTEND_DIR" status --porcelain --untracked-files=no -- . ':(exclude)node_modules' ':(exclude)dist')" ]] || return 1
  canonical="${TARGET_VERSION}|${backend_commit}|${frontend_commit}"
  expected="$(printf '%s' "$canonical" | openssl dgst -sha256 -mac HMAC -macopt "hexkey:$(printf '%s' "$license_key" | sha256sum | awk '{print $1}')" | awk '{print $2}')"
  [[ "$signature" == "$expected" ]] || return 1

  git -C "$BACKEND_DIR" fetch origin "$backend_commit"
  git -C "$FRONTEND_DIR" fetch origin "$frontend_commit"
  git -C "$BACKEND_DIR" cat-file -e "${backend_commit}^{commit}"
  git -C "$FRONTEND_DIR" cat-file -e "${frontend_commit}^{commit}"
  OLD_BACKEND="$(git -C "$BACKEND_DIR" rev-parse HEAD)"
  OLD_FRONTEND="$(git -C "$FRONTEND_DIR" rev-parse HEAD)"
  git -C "$BACKEND_DIR" merge-base --is-ancestor "$OLD_BACKEND" "$backend_commit"
  git -C "$FRONTEND_DIR" merge-base --is-ancestor "$OLD_FRONTEND" "$frontend_commit"
  git -C "$BACKEND_DIR" show "${backend_commit}:src/version.py" | \
    grep -Eq "^SYSTEM_VERSION = [\"']${TARGET_VERSION}[\"']$"
  create_backup
  UPDATE_ACTIVE="true"
  write_status "iniciando" "Aplicando versión ${TARGET_VERSION}"
  report "iniciando" "Aplicando versión ${TARGET_VERSION}"
  systemctl stop fdeznet-api fdeznet-bot
  git -C "$BACKEND_DIR" reset --hard "$backend_commit"
  git -C "$FRONTEND_DIR" reset --hard "$frontend_commit"
  install -o root -g root -m 0750 "$BACKEND_DIR/scripts/fdeznet-maintenance.sh" /usr/local/sbin/fdeznet-maintenance
  chown -R fdeznet:fdeznet "$BACKEND_DIR" "$FRONTEND_DIR"
  runuser -u fdeznet -- "$BACKEND_DIR/venv/bin/pip" install -r "$BACKEND_DIR/requirements.txt"
  (cd "$BACKEND_DIR" && runuser -u fdeznet -- ./venv/bin/alembic upgrade head)
  runuser -u fdeznet -- npm --prefix "$BACKEND_DIR/bot_whatsapp" ci --omit=dev
  runuser -u fdeznet -- npm --prefix "$FRONTEND_DIR" ci
  runuser -u fdeznet -- npm --prefix "$FRONTEND_DIR" run build
  systemctl restart fdeznet-api fdeznet-bot nginx
  wait_for_health
  UPDATE_ACTIVE="false"
  write_status "exitosa" "Versión ${TARGET_VERSION} instalada y verificada"
  report "exitosa" "Actualización instalada y verificada"
}

main() {
  [[ -f "$ENV_FILE" && -f "$KEY_FILE" ]] || { log "Mantenimiento no configurado"; exit 0; }
  BACKUP_DIR="$(env_value FDEZNET_BACKUP_DIR)"
  BACKUP_DIR="${BACKUP_DIR:-/var/backups/fdeznet}"
  RETENTION_DAYS="$(env_value FDEZNET_BACKUP_RETENTION_DAYS)"
  RETENTION_DAYS="${RETENTION_DAYS:-14}"
  REMOTE_DIR="$(env_value FDEZNET_BACKUP_REMOTE_DIR)"
  if [[ -f "$STATUS_FILE" ]] && jq -e . "$STATUS_FILE" >/dev/null 2>&1; then
    RECOVERY_STATUS="$(jq -r '.recuperacion_estado // "sin_revision"' "$STATUS_FILE")"
    RECOVERY_DATE="$(jq -r '.recuperacion_fecha // ""' "$STATUS_FILE")"
  fi
  [[ "$RETENTION_DAYS" =~ ^[0-9]{1,3}$ ]] || fail "Retención inválida"
  exec 9> "$LOCK_FILE"
  flock -n 9 || { log "Ya existe otra tarea de mantenimiento"; exit 0; }
  case "${1:-}" in
    backup) create_backup ;;
    update) perform_update ;;
    verify) verify_backup "${2:-}" ;;
    restore) [[ -n "${2:-}" ]] || return 2; restore_backup "$2" ;;
    *) printf 'Uso: %s {backup|update|verify [ARCHIVO]|restore ARCHIVO}\n' "$0"; return 2 ;;
  esac
}

main "$@"
