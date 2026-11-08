#!/usr/bin/env bash
# Activa el ACS (TR-069) de un ISP: su base en el Mongo compartido, usuario y
# clave para los módems, el puerto público y su contenedor. Uso:
#   nube/activar-acs.sh <isp> <puerto> <dominio>
# Si ya estaba activo conserva sus credenciales (los módems ya las tienen) y
# solo corrige lo que falte. ARRANCAR=0 lo deja configurado sin levantarlo
# (ISP suspendido). Imprime ACS_URL, ACS_USUARIO y ACS_CLAVE.
set -Eeuo pipefail

ISP="${1:?Falta el nombre del ISP}"
PUERTO="${2:?Falta el puerto del ACS}"
DOMINIO="${3:?Falta el dominio del ISP}"
[[ "$ISP" =~ ^[a-z][a-z0-9-]{1,30}$ ]] || { echo "Nombre de ISP inválido: $ISP" >&2; exit 1; }
[[ "$PUERTO" =~ ^[0-9]{4,5}$ ]] || { echo "Puerto inválido: $PUERTO" >&2; exit 1; }
[[ "$DOMINIO" =~ ^[a-z0-9.-]+$ ]] || { echo "Dominio inválido: $DOMINIO" >&2; exit 1; }
DIR="$(cd "$(dirname "$0")" && pwd)"
INST="$DIR/instancias/$ISP"
[[ -f "$INST/compose.env" && -f "$INST/api.env" ]] || { echo "No existe la instancia $ISP" >&2; exit 1; }
if grep -qsx "ACS_PORT=$PUERTO" "$DIR"/instancias/*/compose.env \
   && ! grep -qsx "ACS_PORT=$PUERTO" "$INST/compose.env"; then
  echo "El puerto $PUERTO ya lo usa otro ISP" >&2; exit 1
fi

clave() { openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c "${1:-24}"; }
leer() { { sed -n "s/^$2=//p" "$1" 2>/dev/null || true; } | tail -1; }
poner() {  # archivo clave valor
  if grep -q "^$2=" "$1"; then sed -i "s|^$2=.*|$2=$3|" "$1"; else printf '%s=%s\n' "$2" "$3" >> "$1"; fi
}
umask 077

# 1. Mongo compartido (la primera vez se crea con su clave de root).
install -d -m 0700 "$DIR/secretos"
[[ -s "$DIR/secretos/mongo_root" ]] || clave 40 > "$DIR/secretos/mongo_root"
# Mongo lee su secreto con su propio usuario (999); la carpeta secretos/ (0700)
# es la que lo protege en la VPS.
chmod 0444 "$DIR/secretos/mongo_root"
mongo=(docker compose -f "$DIR/compose.mongo.yml")
"${mongo[@]}" up -d >/dev/null
mongosh_root() {
  "${mongo[@]}" exec -T mongo sh -c \
    'mongosh --quiet -u root -p "$(cat /run/secrets/mongo_root)" --authenticationDatabase admin --eval "$1"' _ "$1"
}
for intento in $(seq 1 40); do
  if mongosh_root 'db.runCommand({ping: 1}).ok' 2>/dev/null | grep -qx 1; then break; fi
  [[ "$intento" -lt 40 ]] || { echo "Mongo no respondió" >&2; exit 1; }
  sleep 3
done

# 2. Credenciales: las de antes si ya tenía ACS (los módems ya las traen).
BASE="acs_${ISP//-/_}"
ACS_ENV="$INST/acs.env"
MONGO_CLAVE="$(leer "$ACS_ENV" MONGO_CLAVE)";  MONGO_CLAVE="${MONGO_CLAVE:-$(clave 32)}"
ACS_USUARIO="$(leer "$ACS_ENV" ACS_USUARIO)";  ACS_USUARIO="${ACS_USUARIO:-$ISP-acs}"
ACS_CLAVE="$(leer "$ACS_ENV" ACS_CLAVE)";      ACS_CLAVE="${ACS_CLAVE:-$(clave 20)}"
CR_USUARIO="$(leer "$ACS_ENV" CR_USUARIO)";    CR_USUARIO="${CR_USUARIO:-$ISP-cr}"
CR_CLAVE="$(leer "$ACS_ENV" CR_CLAVE)";        CR_CLAVE="${CR_CLAVE:-$(clave 24)}"

mongosh_root "
const base = db.getSiblingDB('$BASE');
if (base.getUser('$BASE')) { base.updateUser('$BASE', {pwd: '$MONGO_CLAVE'}); }
else { base.createUser({user: '$BASE', pwd: '$MONGO_CLAVE', roles: [{role: 'readWrite', db: '$BASE'}]}); }
" >/dev/null

cat > "$ACS_ENV" <<ENV
GENIEACS_MONGODB_CONNECTION_URL=mongodb://$BASE:$MONGO_CLAVE@mongo:27017/$BASE?authSource=$BASE
MONGO_CLAVE=$MONGO_CLAVE
ACS_USUARIO=$ACS_USUARIO
ACS_CLAVE=$ACS_CLAVE
CR_USUARIO=$CR_USUARIO
CR_CLAVE=$CR_CLAVE
ENV

# 3. Puerto y perfil del contenedor; la API usa este ACS y muestra sus datos.
ACS_URL="http://$DOMINIO:$PUERTO"
poner "$INST/compose.env" ACS_PORT "$PUERTO"
PERFILES="$(leer "$INST/compose.env" COMPOSE_PROFILES)"
[[ ",$PERFILES," == *,acs,* ]] || PERFILES="${PERFILES:+$PERFILES,}acs"
poner "$INST/compose.env" COMPOSE_PROFILES "$PERFILES"
poner "$INST/api.env" GENIEACS_NBI_URL "http://acs:7557"
poner "$INST/api.env" ACS_URL "$ACS_URL"
poner "$INST/api.env" ACS_USUARIO "$ACS_USUARIO"
poner "$INST/api.env" ACS_CLAVE "$ACS_CLAVE"

if [[ "${ARRANCAR:-1}" == "1" ]]; then
  docker compose -p "$ISP" --env-file "$INST/compose.env" -f "$DIR/compose.isp.yml" up -d --remove-orphans
fi

echo "ACS_URL=$ACS_URL"
echo "ACS_USUARIO=$ACS_USUARIO"
echo "ACS_CLAVE=$ACS_CLAVE"
