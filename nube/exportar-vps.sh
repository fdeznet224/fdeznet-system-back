#!/usr/bin/env bash
# Corre en la VPS de un ISP instalado con install.sh, como root. Tres usos:
#
#   exportar-vps.sh [--sin-detener] [--sin-whatsapp]
#       Detiene el sistema (API, bot y tareas) y deja todo en
#       /root/wispay-exportacion-AAAAMMDD-HHMMSS.tar para nube/importar-vps.sh.
#       --sin-detener: copia en vivo, sin apagar nada (para ensayar).
#       --sin-whatsapp: sin la sesión de WhatsApp (en la nube se escanea de nuevo).
#
#   exportar-vps.sh --retirar <subdominio.en.la.nube>
#       Ya funcionando en la nube: apaga para siempre el sistema de esta VPS
#       (sin borrar nada) y manda su dirección vieja a la nueva.
#
#   exportar-vps.sh --regresar
#       Si algo salió mal en la nube: vuelve a encender el sistema de esta VPS.
set -Eeuo pipefail

APP=/opt/fdeznet/backend
SERVICIOS=(fdeznet-api fdeznet-bot)
TAREAS=(fdeznet-backup.timer fdeznet-update.timer fdeznet-verify.timer
        fdeznet-backup-request.path fdeznet-update-request.path fdeznet-verify-latest-request.path
        fdeznet-verify-selected-request.path fdeznet-restore-request.path)

[[ $EUID -eq 0 ]] || { echo "Corre este script como root" >&2; exit 1; }
[[ -f "$APP/.env" ]] || { echo "No hay instalación en $APP" >&2; exit 1; }

valor() { sed -n "s/^$1=//p" "$APP/.env" | tail -1 | sed -e 's/^["'\'']//' -e 's/["'\'']$//'; }

if [[ "${1:-}" == "--regresar" ]]; then
  systemctl enable --now wg-quick@wg0 "${SERVICIOS[@]}" "${TAREAS[@]}" 2>/dev/null || true
  if [[ -f /etc/nginx/sites-available/fdeznet.antes-nube ]]; then
    mv /etc/nginx/sites-available/fdeznet.antes-nube /etc/nginx/sites-available/fdeznet
    nginx -t && systemctl reload nginx
  fi
  echo "Sistema de esta VPS encendido otra vez. Regresa el endpoint de los MikroTik a esta IP."
  exit 0
fi

if [[ "${1:-}" == "--retirar" ]]; then
  NUEVO="${2:?Falta el subdominio en la nube, p. ej. colega.wispay.click}"
  [[ "$NUEVO" =~ ^[a-z0-9.-]+$ ]] || { echo "Dominio inválido: $NUEVO" >&2; exit 1; }
  systemctl disable --now "${SERVICIOS[@]}" "${TAREAS[@]}" wg-quick@wg0 2>/dev/null || true
  SITIO=/etc/nginx/sites-available/fdeznet
  if [[ -f "$SITIO" && ! -f "$SITIO.antes-nube" ]]; then
    cp -a "$SITIO" "$SITIO.antes-nube"
    # Se queda con su dominio y sus certificados; solo responde con la dirección nueva.
    NOMBRE="$(sed -n 's/^[[:space:]]*server_name[[:space:]]*\([^;]*\);.*/\1/p' "$SITIO" | head -1)"
    CERT="$(sed -n 's/^[[:space:]]*ssl_certificate[[:space:]]*\([^;]*\);.*/\1/p' "$SITIO" | head -1)"
    LLAVE="$(sed -n 's/^[[:space:]]*ssl_certificate_key[[:space:]]*\([^;]*\);.*/\1/p' "$SITIO" | head -1)"
    {
      printf 'server {\n    listen 80;\n    listen [::]:80;\n    server_name %s;\n    return 301 https://%s$request_uri;\n}\n' "${NOMBRE:-_}" "$NUEVO"
      if [[ -n "$CERT" && -n "$LLAVE" ]]; then
        printf 'server {\n    listen 443 ssl;\n    listen [::]:443 ssl;\n    server_name %s;\n    ssl_certificate %s;\n    ssl_certificate_key %s;\n    return 301 https://%s$request_uri;\n}\n' \
          "${NOMBRE:-_}" "$CERT" "$LLAVE" "$NUEVO"
      fi
    } > "$SITIO"
    if nginx -t 2>/dev/null; then
      systemctl reload nginx
    else
      cp -a "$SITIO.antes-nube" "$SITIO" && rm -f "$SITIO.antes-nube"
      echo "AVISO: Nginx no aceptó la redirección; se dejó el sitio como estaba (el sistema sigue apagado)" >&2
    fi
  fi
  echo "Listo: esta VPS ya no corre el sistema y su dirección manda a https://$NUEVO"
  echo "Los datos siguen en $APP y en la base; --regresar lo vuelve a encender."
  exit 0
fi

DETENER=1
WHATSAPP=1
for opcion in "$@"; do
  case "$opcion" in
    --sin-detener) DETENER=0 ;;
    --sin-whatsapp) WHATSAPP=0 ;;
    *) echo "Opción desconocida: $opcion" >&2; exit 1 ;;
  esac
done

# El nombre de la base viene en DB_NAME o al final de DATABASE_URL (…@host/base).
BASE="$(valor DB_NAME)"
[[ -n "$BASE" ]] || BASE="$(valor DATABASE_URL | sed -n 's#.*/\([A-Za-z0-9_]*\)\([?].*\)\{0,1\}$#\1#p')"
BASE="${BASE:-fdeznet_db}"
SELLO="$(date +%Y%m%d-%H%M%S)"
ARCHIVO="/root/wispay-exportacion-$SELLO.tar"
umask 077
TMP="$(mktemp -d /root/.wispay-exportando-XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

if [[ "$DETENER" == "1" ]]; then
  # Desde aquí lo que pase (pagos, mensajes) ya no se pierde: el sistema queda quieto.
  echo "Deteniendo el sistema de esta VPS…"
  systemctl stop "${TAREAS[@]}" 2>/dev/null || true
  systemctl stop "${SERVICIOS[@]}"
fi

# 1. Base de datos. root entra por el socket de MySQL/MariaDB.
echo "Copiando la base $BASE…"
opciones=(--single-transaction --routines --triggers --events --no-tablespaces)
mysqldump --help 2>/dev/null | grep -q -- '--set-gtid-purged' && opciones+=(--set-gtid-purged=OFF)
mysqldump "${opciones[@]}" "$BASE" | gzip > "$TMP/base.sql.gz"
REVISION="$(mysql -N -B -e "SELECT version_num FROM alembic_version LIMIT 1" "$BASE" 2>/dev/null || true)"

# 2. Archivos, con el nombre del volumen al que van en la nube.
lista="$TMP/lista"
: > "$lista"
for ruta in etc/wireguard opt/fdeznet/backend/uploads opt/fdeznet/backend/bot_whatsapp/uploads; do
  [[ -e "/$ruta" ]] && printf '%s\0' "$ruta" >> "$lista"
done
# De /var/lib/fdeznet solo los ajustes sueltos; sus carpetas son del mantenimiento
# de esta VPS (respaldos, actualizaciones) y en la nube no se usan.
find /var/lib/fdeznet -maxdepth 1 -type f -printf 'var/lib/fdeznet/%P\0' 2>/dev/null >> "$lista" || true
if [[ "$WHATSAPP" == "1" && -d "$APP/bot_whatsapp/.wwebjs_auth" ]]; then
  printf '%s\0' opt/fdeznet/backend/bot_whatsapp/.wwebjs_auth >> "$lista"
fi
# De static solo lo que subió el ISP (logos, recibos): lo del código ya viene en la nube.
if git -c safe.directory="$APP" -C "$APP" rev-parse --git-dir >/dev/null 2>&1; then
  git -c safe.directory="$APP" -C "$APP" ls-files -o -z -- static | while IFS= read -r -d '' f; do
    printf '%s\0' "opt/fdeznet/backend/$f"
  done >> "$lista"
fi
echo "Copiando archivos…"
estado_tar=0
tar czf "$TMP/volumenes.tar.gz" \
  --warning=no-file-changed --warning=no-file-removed \
  --exclude='Cache' --exclude='Code Cache' --exclude='GPUCache' --exclude='CacheStorage' \
  --exclude='ScriptCache' --exclude='Singleton*' \
  --transform='s,^etc/wireguard,wireguard,S' \
  --transform='s,^opt/fdeznet/backend/bot_whatsapp/uploads,whatsapp-uploads,S' \
  --transform='s,^opt/fdeznet/backend/bot_whatsapp/\.wwebjs_auth,whatsapp-sesion,S' \
  --transform='s,^opt/fdeznet/backend/uploads,uploads,S' \
  --transform='s,^opt/fdeznet/backend/static,static,S' \
  --transform='s,^var/lib/fdeznet,estado,S' \
  -C / --null -T "$lista" || estado_tar=$?
(( estado_tar <= 1 )) || { echo "Falló la copia de archivos (tar $estado_tar)" >&2; exit "$estado_tar"; }

# 3. Los secretos que la nube necesita para leer esta base: SECRET_KEY cifra las
#    contraseñas de routers y OLT. Lo demás (base, dominio, WhatsApp) lo pone la nube.
for clave in SECRET_KEY ALGORITHM ACCESS_TOKEN_EXPIRE_MINUTES TECNICO_TOKEN_EXPIRE_MINUTES \
             LOGIN_MAX_ATTEMPTS LOGIN_WINDOW_SECONDS VAPID_PUBLIC_KEY VAPID_PRIVATE_KEY VAPID_SUBJECT \
             ONU_WEB_CREDENCIALES ONU_DESCRIPCION_AUTOMATICA OLT_GUARDAR_CONFIGURACION WIFI_PREFIJO; do
  v="$(valor "$clave")"
  [[ -n "$v" ]] && printf '%s=%s\n' "$clave" "$v"
done > "$TMP/secretos.env"
grep -q '^SECRET_KEY=' "$TMP/secretos.env" || { echo "No se encontró SECRET_KEY en $APP/.env" >&2; exit 1; }

RED="$(sed -n 's/^Address *= *\([0-9]*\.[0-9]*\.[0-9]*\)\.[0-9]*\/.*/\1/p' /etc/wireguard/wg0.conf 2>/dev/null | head -1)"
{
  echo "ORIGEN=$(hostname)"
  echo "FECHA=$(date -Iseconds)"
  echo "DETENIDO=$DETENER"
  echo "WHATSAPP=$WHATSAPP"
  echo "VERSION=$(sed -n 's/^SYSTEM_VERSION *= *"\(.*\)"/\1/p' "$APP/src/version.py" 2>/dev/null)"
  echo "REVISION=$REVISION"
  echo "DOMINIO=$(valor PUBLIC_URL)"
  echo "VPN_RED=${RED:-10.8.0}"
  echo "VPN_PUBLICA=$(cat /etc/wireguard/server_public.key 2>/dev/null)"
} > "$TMP/exportacion.txt"

rm -f "$lista"
tar cf "$ARCHIVO" -C "$TMP" .
echo
cat "$TMP/exportacion.txt"
echo "ARCHIVO=$ARCHIVO"
echo "TAMANO=$(du -h "$ARCHIVO" | cut -f1)"
if [[ "$DETENER" == "1" ]]; then
  echo "El sistema de esta VPS quedó detenido. Si no sigues con la mudanza: $0 --regresar"
fi
