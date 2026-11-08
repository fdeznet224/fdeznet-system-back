#!/usr/bin/env bash
# Borra para siempre a un ISP de la VPS: contenedores, volúmenes, base, usuario
# de MySQL, secretos y respaldos locales. Lo usa el panel con los demos que
# terminaron sin pagar. Uso:
#   nube/borrar-instancia.sh <isp>
set -Eeuo pipefail

ISP="${1:?Falta el nombre del ISP}"
[[ "$ISP" =~ ^[a-z][a-z0-9-]{1,30}$ ]] || { echo "Nombre de ISP inválido: $ISP" >&2; exit 1; }
DIR="$(cd "$(dirname "$0")" && pwd)"
INST="$DIR/instancias/$ISP"
BASE="isp_${ISP//-/_}"

if [[ -f "$INST/compose.env" ]]; then
  docker compose -p "$ISP" --env-file "$INST/compose.env" -f "$DIR/compose.isp.yml" \
    --profile whatsapp --profile acs down --volumes --remove-orphans
fi
# Los volúmenes compartidos (modelos de OCR) no llevan el prefijo del ISP.
docker volume ls -q --filter "name=^${ISP}_" | xargs -r docker volume rm >/dev/null
docker compose -f "$DIR/compose.mysql.yml" exec -T mysql sh -c \
  'MYSQL_PWD="$(cat /run/secrets/mysql_root)" mysql -uroot -e "DROP DATABASE IF EXISTS \`$1\`; DROP USER IF EXISTS \`$1\`@\`%\`"' _ "$BASE"
# Su base del ACS (si lo activó) en el Mongo compartido.
if [[ -f "$INST/acs.env" && -s "$DIR/secretos/mongo_root" ]]; then
  ACS_BASE="acs_${ISP//-/_}"
  docker compose -f "$DIR/compose.mongo.yml" exec -T mongo sh -c \
    'mongosh --quiet -u root -p "$(cat /run/secrets/mongo_root)" --authenticationDatabase admin --eval "
       const b = db.getSiblingDB(\"$1\"); if (b.getUser(\"$1\")) b.dropUser(\"$1\"); b.dropDatabase();"' _ "$ACS_BASE" \
    >/dev/null || echo "AVISO: no se pudo borrar la base del ACS $ACS_BASE" >&2
fi
rm -rf "$INST" "$DIR/respaldos/$ISP" "$DIR/secretos/drive/$ISP.conf"
echo "BORRADO=$ISP"
