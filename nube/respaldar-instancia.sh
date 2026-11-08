#!/usr/bin/env bash
# Respalda un ISP en un solo archivo: su base (mysqldump), sus volúmenes
# (WireGuard, archivos subidos, sesión de WhatsApp) y sus secretos. Uso:
#   nube/respaldar-instancia.sh <isp>
# Queda en nube/respaldos/<isp>/<isp>-AAAAMMDD-HHMMSS.tar. Opcionales por entorno
# (los pasa el panel central):
#   RESPALDOS_LOCALES=7       cuántos se quedan en la VPS
#   RESPALDO_REMOTO=r2:fdezpay-respaldos   copia fuera de la VPS con rclone
#                             (configurado en secretos/rclone.conf o en RCLONE_CONFIG;
#                             el panel pasa el Drive del ISP si lo conectó)
#   RESPALDO_DIAS_REMOTO=30   cuántos días se guardan fuera
set -Eeuo pipefail

ISP="${1:?Falta el nombre del ISP}"
[[ "$ISP" =~ ^[a-z][a-z0-9-]{1,30}$ ]] || { echo "Nombre de ISP inválido: $ISP" >&2; exit 1; }
DIR="$(cd "$(dirname "$0")" && pwd)"
INST="$DIR/instancias/$ISP"
[[ -d "$INST" ]] || { echo "No existe la instancia $ISP" >&2; exit 1; }

BASE="isp_${ISP//-/_}"
DESTINO="$DIR/respaldos/$ISP"
SELLO="$(date +%Y%m%d-%H%M%S)"
ARCHIVO="$DESTINO/$ISP-$SELLO.tar"
umask 077
install -d -m 0700 "$DIR/respaldos" "$DESTINO"
TMP="$(mktemp -d "$DESTINO/.armando-XXXXXX")"
BOT="${ISP}-bot-1"
BOT_PAUSADO=""
limpiar() {
  [[ -n "$BOT_PAUSADO" ]] && docker unpause "$BOT" >/dev/null 2>&1
  rm -rf "$TMP"
}
trap limpiar EXIT

# 1. Base: --single-transaction da una foto consistente sin detener la API.
docker compose -f "$DIR/compose.mysql.yml" exec -T mysql sh -c \
  'MYSQL_PWD="$(cat /run/secrets/mysql_root)" exec mysqldump -uroot --single-transaction \
     --routines --triggers --events --no-tablespaces --set-gtid-purged=OFF "$1"' _ "$BASE" \
  | gzip > "$TMP/base.sql.gz"

# 2. Volúmenes (los de la API y el bot; no los modelos de OCR ni la caché de Chrome).
montajes=()
for volumen in wireguard static uploads whatsapp-uploads whatsapp-sesion estado; do
  if docker volume inspect "${ISP}_$volumen" >/dev/null 2>&1; then
    montajes+=(-v "${ISP}_$volumen:/datos/$volumen:ro")
  fi
done
# Chrome del bot escribe su sesión todo el tiempo; se congela mientras se copia
# (docker pause no corta la conexión de WhatsApp) para que quede completa.
if [[ "$(docker inspect -f '{{.State.Running}} {{.State.Paused}}' "$BOT" 2>/dev/null)" == "true false" ]]; then
  docker pause "$BOT" >/dev/null && BOT_PAUSADO=1
fi
# tar sale con 1 si un archivo cambió mientras lo leía (un recibo que se está
# subiendo): la copia sirve igual. Solo 2 o más es un error de verdad.
estado_tar=0
docker run --rm --network none "${montajes[@]}" -v "$TMP:/salida" --entrypoint tar mysql:8.4 \
  czf /salida/volumenes.tar.gz -C /datos --warning=no-file-changed --warning=no-file-removed \
  --exclude='Cache' --exclude='Code Cache' --exclude='GPUCache' --exclude='CacheStorage' \
  --exclude='ScriptCache' --exclude='Singleton*' . || estado_tar=$?
if [[ -n "$BOT_PAUSADO" ]]; then
  docker unpause "$BOT" >/dev/null && BOT_PAUSADO=""
fi
(( estado_tar <= 1 )) || { echo "Falló la copia de los volúmenes (tar $estado_tar)" >&2; exit "$estado_tar"; }

# 3. Secretos de la instancia (para llevarla a otra VPS) y una ficha legible.
cp "$INST/compose.env" "$INST/api.env" "$TMP/"
# El ACS: solo sus credenciales; sus equipos se vuelven a reportar solos.
[[ ! -f "$INST/acs.env" ]] || cp "$INST/acs.env" "$TMP/"
{
  echo "ISP=$ISP"
  echo "FECHA=$(date -Iseconds)"
  grep -E '^IMAGEN_VERSION=' "$INST/compose.env" || true
  echo "BASE=$BASE"
} > "$TMP/respaldo.txt"

tar cf "$ARCHIVO.parcial" -C "$TMP" .
mv "$ARCHIVO.parcial" "$ARCHIVO"

# 4. Solo los últimos N en la VPS.
ls -1t "$DESTINO"/"$ISP"-*.tar | tail -n +"$(( ${RESPALDOS_LOCALES:-7} + 1 ))" | xargs -r rm -f

echo "ARCHIVO=$(basename "$ARCHIVO")"
echo "TAMANO=$(stat -c %s "$ARCHIVO")"

# 5. Copia fuera de la VPS. Si falla, el respaldo local sigue siendo válido.
if [[ -z "${RESPALDO_REMOTO:-}" ]]; then
  echo "REMOTO=no"
elif ! command -v rclone >/dev/null; then
  echo "REMOTO=error: rclone no está instalado"
else
  export RCLONE_CONFIG="${RCLONE_CONFIG:-$DIR/secretos/rclone.conf}"
  if error="$(rclone copy "$ARCHIVO" "$RESPALDO_REMOTO/$ISP/" 2>&1 \
       && rclone delete --min-age "${RESPALDO_DIAS_REMOTO:-30}d" "$RESPALDO_REMOTO/$ISP/" 2>&1)"; then
    echo "REMOTO=ok"
  else
    echo "REMOTO=error: $(tail -n 1 <<<"$error")"
  fi
fi
