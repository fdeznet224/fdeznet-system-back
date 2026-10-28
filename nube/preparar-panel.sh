#!/usr/bin/env bash
# Crea secretos/panel.env con tu usuario y contraseña del panel central.
#   nube/preparar-panel.sh <dominio_base> [usuario]
set -Eeuo pipefail
DOMINIO="${1:?Falta el dominio base, ej. fdezpay.com}"
USUARIO="${2:-dueno}"
DIR="$(cd "$(dirname "$0")" && pwd)"
install -d -m 0700 "$DIR/secretos"
read -rsp "Contraseña del panel (mínimo 12 caracteres): " CLAVE; echo
[[ ${#CLAVE} -ge 12 ]] || { echo "Muy corta" >&2; exit 1; }
HASH="$(docker run --rm -i fdezpay-panel:local python -c \
  'import sys; from passlib.hash import pbkdf2_sha256; print(pbkdf2_sha256.hash(sys.stdin.read().rstrip("\n")))' <<<"$CLAVE" | tr -d '\n')"
umask 077
cat > "$DIR/secretos/panel.env" <<ENV
PANEL_USUARIO=$USUARIO
# Entre comillas simples: compose no debe leer los $ del hash como variables.
PANEL_CLAVE_HASH='$HASH'
PANEL_SECRETO=$(openssl rand -hex 32)
PANEL_DOMINIO_BASE=$DOMINIO
PANEL_DIAS_GRACIA=5
# Respaldo diario de cada ISP (vacío: ninguno) y cuántos se quedan en la VPS.
PANEL_HORA_RESPALDO=03:00
PANEL_RESPALDOS_LOCALES=7
# Copia fuera de la VPS con rclone (configurado en secretos/rclone.conf), p. ej.
# r2-cifrado:  — vacío: solo en la VPS.
PANEL_RESPALDO_REMOTO=
PANEL_RESPALDO_DIAS_REMOTO=30
NUBE_DIR=$DIR
ENV
echo "Listo: $DIR/secretos/panel.env"
