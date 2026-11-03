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
# Corte automático tras la gracia (vacío: ninguno) y días de prórroga al reactivar a un moroso.
PANEL_HORA_CORTE=09:00
PANEL_DIAS_PRORROGA=3
# Aviso en el admin del ISP: cuántos días antes de vencer y a quién pagarle (p. ej. "WhatsApp 55 1234 5678").
PANEL_DIAS_AVISO=5
PANEL_CONTACTO_PAGO=
# WhatsApp de ventas (contenedor bot del panel) y demos de la página de registro.
PANEL_BOT_VERSION=$(sed -n 's/^SYSTEM_VERSION *= *"\(.*\)"/\1/p' "$(dirname "$0")/../src/version.py")
PANEL_BOT_SECRETO=$(openssl rand -hex 32)
PANEL_DEMO_DIAS=30
PANEL_DEMO_GRACIA=15
PANEL_DEMOS_MAX=20
# Respaldo diario de cada ISP (vacío: ninguno) y cuántos se quedan en la VPS.
PANEL_HORA_RESPALDO=03:00
PANEL_RESPALDOS_LOCALES=7
# Copia fuera de la VPS con rclone (configurado en secretos/rclone.conf), p. ej.
# r2-cifrado:  — vacío: solo en la VPS.
PANEL_RESPALDO_REMOTO=
PANEL_RESPALDO_DIAS_REMOTO=30
NUBE_DIR=$DIR
# IP pública a la que se conectan los MikroTik de cada ISP (WireGuard).
VPN_SERVER_IP=$(curl -fsS https://api.ipify.org || true)
ENV
echo "Listo: $DIR/secretos/panel.env"
