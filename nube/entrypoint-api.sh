#!/usr/bin/env bash
# Arranque de la API de un ISP: su WireGuard, migraciones, admin inicial y uvicorn.
set -Eeuo pipefail

WG_DIR=/etc/wireguard
WG_CONF="$WG_DIR/${WG_INTERFACE}.conf"
WG_PORT="${WG_PORT:-51820}"

# 1. WireGuard propio del ISP. Las llaves y peers viven en el volumen de
#    /etc/wireguard, así que sobreviven a reinicios y actualizaciones.
if [[ ! -s "$WG_CONF" ]]; then
  echo "[nube] Creando WireGuard ${WG_INTERFACE} en el puerto ${WG_PORT}"
  umask 077
  wg genkey > "$WG_DIR/server_private.key"
  wg pubkey < "$WG_DIR/server_private.key" > "$WG_DIR/server_public.key"
  cat > "$WG_CONF" <<CONF
[Interface]
Address = 10.9.0.1/24
ListenPort = ${WG_PORT}
PrivateKey = $(cat "$WG_DIR/server_private.key")
SaveConfig = true
CONF
fi
# El puerto lo decide el panel; si cambió, se corrige en el archivo.
sed -i "s/^ListenPort = .*/ListenPort = ${WG_PORT}/" "$WG_CONF"
wg-quick down "$WG_INTERFACE" >/dev/null 2>&1 || true
if ! wg-quick up "$WG_INTERFACE"; then
  echo "[nube] AVISO: no se pudo levantar WireGuard; la API arranca sin VPN" >&2
fi

# 2. Base de datos: esperar a MySQL. Una base vacía se crea con el esquema
#    actual y se registra en Alembic (como install.sh); una existente se migra.
for intento in $(seq 1 60); do
  if ESTADO_BASE="$(python /app/nube/preparar_base.py | tail -1)"; then break; fi
  [[ "$intento" -lt 60 ]] || { echo "[nube] La base no respondió" >&2; exit 1; }
  echo "[nube] Esperando la base de datos (${intento}/60)"
  sleep 3
done
case "$ESTADO_BASE" in
  fresh) echo "[nube] Base nueva creada"; alembic stamp head ;;
  existing) alembic upgrade head ;;
  *) echo "[nube] Estado de base desconocido: $ESTADO_BASE" >&2; exit 1 ;;
esac

# 3. Administrador inicial: solo la primera vez (crear_usuario falla si ya existe).
if [[ -n "${ADMIN_BOOTSTRAP_PASSWORD:-}" ]]; then
  python -m src.crear_admin || echo "[nube] El administrador inicial ya existía"
fi

exec uvicorn src.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'
