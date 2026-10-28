#!/usr/bin/env bash
# Da de alta un ISP en la nube: base y usuario propios en el MySQL compartido,
# secretos, y sus contenedores. Uso:
#   nube/crear-instancia.sh <isp> <puerto_web> <puerto_wireguard> <dominio>
# Ejemplo: nube/crear-instancia.sh prueba 18001 51821 prueba.fdezpay.com
# Opcionales por entorno (los usa el panel central): LIMITE_CLIENTES,
# LIMITE_ROUTERS, CON_WHATSAPP=0 e IMAGEN_VERSION.
set -Eeuo pipefail

ISP="${1:?Falta el nombre del ISP (minúsculas, números y guiones)}"
WEB_PORT="${2:?Falta el puerto web local}"
WG_PORT="${3:?Falta el puerto UDP de WireGuard}"
DOMINIO="${4:?Falta el subdominio}"
[[ "$ISP" =~ ^[a-z][a-z0-9-]{1,30}$ ]] || { echo "Nombre de ISP inválido: $ISP" >&2; exit 1; }

DIR="$(cd "$(dirname "$0")" && pwd)"
INST="$DIR/instancias/$ISP"
[[ ! -e "$INST" ]] || { echo "La instancia $ISP ya existe en $INST" >&2; exit 1; }
for puerto in "$WEB_PORT" "$WG_PORT"; do
  if grep -rqsx "\(WEB\|WG\)_PORT=$puerto" "$DIR"/instancias/*/compose.env; then
    echo "El puerto $puerto ya lo usa otra instancia" >&2; exit 1
  fi
done

clave() { openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c "${1:-40}"; }
BASE="isp_${ISP//-/_}"
DB_PASSWORD="$(clave 32)"
WEBHOOK_SECRET="$(clave 48)"
ADMIN_PASSWORD="$(clave 16)"
PERFILES=""
[[ "${CON_WHATSAPP:-1}" != "1" ]] || PERFILES=whatsapp

# 1. Base y usuario en el MySQL compartido (solo con permisos sobre su base).
docker compose -f "$DIR/compose.mysql.yml" exec -T mysql sh -c \
  'mysql -uroot -p"$(cat /run/secrets/mysql_root)"' <<SQL
CREATE DATABASE \`$BASE\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER '$BASE'@'%' IDENTIFIED BY '$DB_PASSWORD';
GRANT ALL PRIVILEGES ON \`$BASE\`.* TO '$BASE'@'%';
SQL

# 2. Secretos de la instancia.
install -d -m 0700 "$INST"
umask 077
cat > "$INST/compose.env" <<ENV
ISP=$ISP
WEB_PORT=$WEB_PORT
WG_PORT=$WG_PORT
WEBHOOK_SECRET=$WEBHOOK_SECRET
IMAGEN_VERSION=${IMAGEN_VERSION:-local}
# Vacío si el ISP no usa el bot de WhatsApp.
COMPOSE_PROFILES=$PERFILES
ENV
cat > "$INST/api.env" <<ENV
ENVIRONMENT=production
DB_HOST=mysql
DB_PORT=3306
DB_NAME=$BASE
DB_USER=$BASE
DB_PASSWORD=$DB_PASSWORD
SECRET_KEY=$(clave 64)
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=60
CORS_ALLOWED_ORIGINS=https://$DOMINIO
PUBLIC_URL=https://$DOMINIO
WEBHOOK_SECRET=$WEBHOOK_SECRET
ADMIN_BOOTSTRAP_USER=admin
ADMIN_BOOTSTRAP_NAME=Super Administrador
ADMIN_BOOTSTRAP_PASSWORD=$ADMIN_PASSWORD
VPN_SUBNET_BASE=10.8.0
VPN_SERVER_IP=${VPN_SERVER_IP:-}
FDEZNET_LIMITE_CLIENTES=${LIMITE_CLIENTES:-}
FDEZNET_LIMITE_ROUTERS=${LIMITE_ROUTERS:-}
ENV

# 3. Contenedores.
docker compose -p "$ISP" --env-file "$INST/compose.env" -f "$DIR/compose.isp.yml" up -d

# 4. Cuando la API ya creó al administrador, su contraseña sale del archivo.
for intento in $(seq 1 100); do
  [[ "$(docker inspect -f '{{.State.Health.Status}}' "$ISP-api-1" 2>/dev/null)" == healthy ]] && break
  [[ "$intento" -lt 100 ]] || { echo "La API de $ISP no quedó lista en 5 minutos" >&2; exit 1; }
  sleep 3
done
sed -i '/^ADMIN_BOOTSTRAP_PASSWORD=/d' "$INST/api.env"

echo
echo "Instancia $ISP creada: web en 127.0.0.1:$WEB_PORT, WireGuard UDP $WG_PORT"
echo "ADMIN_PASSWORD=$ADMIN_PASSWORD"
