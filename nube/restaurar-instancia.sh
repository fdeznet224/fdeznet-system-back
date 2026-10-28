#!/usr/bin/env bash
# Regresa un ISP a como estaba en un respaldo: base y volúmenes. Uso:
#   nube/restaurar-instancia.sh <isp> <archivo.tar>
# Antes respalda el estado actual (por si fue el respaldo equivocado). Los
# secretos de la instancia no se tocan: la base sigue con su usuario y clave.
# ARRANCAR=0 la deja detenida (el panel lo usa con ISP suspendidos).
set -Eeuo pipefail

ISP="${1:?Falta el nombre del ISP}"
ARCHIVO="${2:?Falta el archivo del respaldo}"
[[ "$ISP" =~ ^[a-z][a-z0-9-]{1,30}$ ]] || { echo "Nombre de ISP inválido: $ISP" >&2; exit 1; }
DIR="$(cd "$(dirname "$0")" && pwd)"
INST="$DIR/instancias/$ISP"
[[ -d "$INST" ]] || { echo "No existe la instancia $ISP" >&2; exit 1; }
[[ "$ARCHIVO" == */* ]] || ARCHIVO="$DIR/respaldos/$ISP/$ARCHIVO"
[[ -f "$ARCHIVO" ]] || { echo "No existe el respaldo $ARCHIVO" >&2; exit 1; }

BASE="isp_${ISP//-/_}"
umask 077
install -d -m 0700 "$DIR/respaldos/$ISP"
TMP="$(mktemp -d "$DIR/respaldos/$ISP/.restaurando-XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

# Se extrae primero: el respaldo previo podría borrar este archivo por antigüedad.
tar xf "$ARCHIVO" -C "$TMP"
grep -qx "ISP=$ISP" "$TMP/respaldo.txt" || { echo "El respaldo no es de $ISP" >&2; exit 1; }
[[ -s "$TMP/base.sql.gz" && -s "$TMP/volumenes.tar.gz" ]] || { echo "Respaldo incompleto" >&2; exit 1; }

RESPALDO_REMOTO="" "$DIR/respaldar-instancia.sh" "$ISP" | sed 's/^/previo: /'

compose=(docker compose -p "$ISP" --env-file "$INST/compose.env" -f "$DIR/compose.isp.yml")
"${compose[@]}" --profile whatsapp stop

# 1. Base: se vacía y se carga. Los permisos del usuario del ISP no se pierden.
docker compose -f "$DIR/compose.mysql.yml" exec -T mysql sh -c \
  'export MYSQL_PWD="$(cat /run/secrets/mysql_root)"
   mysql -uroot -e "DROP DATABASE \`$1\`; CREATE DATABASE \`$1\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
   gunzip | mysql -uroot "$1"' _ "$BASE" < "$TMP/base.sql.gz"

# 2. Volúmenes: se vacían y se llenan con lo del respaldo.
montajes=()
for volumen in wireguard static uploads whatsapp-uploads whatsapp-sesion estado; do
  if docker volume inspect "${ISP}_$volumen" >/dev/null 2>&1; then
    montajes+=(-v "${ISP}_$volumen:/datos/$volumen")
  fi
done
docker run --rm --network none "${montajes[@]}" -v "$TMP:/entrada:ro" --entrypoint sh mysql:8.4 -c \
  'for d in /datos/*/; do find "$d" -mindepth 1 -delete; done
   tar xzf /entrada/volumenes.tar.gz -C /datos'

if [[ "${ARRANCAR:-1}" == "1" ]]; then
  "${compose[@]}" up -d
fi
echo "RESTAURADO=$(basename "$ARCHIVO")"
