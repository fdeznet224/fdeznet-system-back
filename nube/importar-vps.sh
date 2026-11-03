#!/usr/bin/env bash
# Muda a la nube un ISP que tenía el sistema en su propia VPS. Uso:
#   nube/importar-vps.sh <isp> <wispay-exportacion-….tar>
# Antes: dar de alta al ISP en el panel (crea la instancia vacía) y correr
# nube/exportar-vps.sh en su VPS. Respalda la instancia antes de tocarla.
#
# Se conserva lo que hace que sus routers sigan igual: la llave del servidor
# WireGuard, su red (10.8.0.x) y el SECRET_KEY que cifra las contraseñas de
# routers y OLT. Al final imprime lo que hay que pegar en cada MikroTik.
# PRUEBA=1: ensayo con una copia (no lleva notificaciones push).
set -Eeuo pipefail

ISP="${1:?Falta el nombre del ISP}"
ARCHIVO="${2:?Falta el archivo de exportación}"
[[ "$ISP" =~ ^[a-z][a-z0-9-]{1,30}$ ]] || { echo "Nombre de ISP inválido: $ISP" >&2; exit 1; }
DIR="$(cd "$(dirname "$0")" && pwd)"
INST="$DIR/instancias/$ISP"
[[ -d "$INST" ]] || { echo "No existe la instancia $ISP; dala de alta primero en el panel" >&2; exit 1; }
[[ -f "$ARCHIVO" ]] || { echo "No existe $ARCHIVO" >&2; exit 1; }

BASE="isp_${ISP//-/_}"
VERSION="$(sed -n 's/^IMAGEN_VERSION=//p' "$INST/compose.env")"
compose=(docker compose -p "$ISP" --env-file "$INST/compose.env" -f "$DIR/compose.isp.yml")
umask 077
install -d -m 0700 "$DIR/respaldos/$ISP"
TMP="$(mktemp -d "$DIR/respaldos/$ISP/.importando-XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

tar xf "$ARCHIVO" -C "$TMP"
for parte in base.sql.gz volumenes.tar.gz secretos.env exportacion.txt; do
  [[ -s "$TMP/$parte" ]] || { echo "Exportación incompleta: falta $parte" >&2; exit 1; }
done
dato() { sed -n "s/^$1=//p" "$TMP/exportacion.txt" | head -1; }
echo "Exportación de $(dato ORIGEN) del $(dato FECHA) · versión $(dato VERSION) · $(dato DOMINIO)"
[[ "$(dato DETENIDO)" == "1" || "${PRUEBA:-0}" == "1" ]] || {
  echo "Esa exportación se hizo con el sistema encendido; para mudar de verdad usa una hecha sin --sin-detener" >&2
  echo "(para ensayar: PRUEBA=1 $0 $ISP $ARCHIVO)" >&2; exit 1; }

# 1. La base tiene que poder migrarse con esta versión (no puede venir de una más nueva).
REVISION="$(dato REVISION)"
if [[ -n "$REVISION" ]] && ! docker run --rm -w /app --entrypoint alembic "fdezpay-api:$VERSION" show "$REVISION" >/dev/null 2>&1; then
  echo "La base viene de una versión que la nube $VERSION no conoce (revisión $REVISION)." >&2
  echo "Construye y asígnale a $ISP una versión igual o más nueva que $(dato VERSION)." >&2
  exit 1
fi

echo "Respaldando $ISP antes de cargarle los datos…"
RESPALDO_REMOTO="" "$DIR/respaldar-instancia.sh" "$ISP" | sed 's/^/previo: /'
"${compose[@]}" --profile whatsapp stop

# 2. Base: los DEFINER apuntan al usuario de la VPS vieja y MariaDB puede traer
#    una intercalación que MySQL 8.4 no tiene.
echo "Cargando la base…"
gunzip -c "$TMP/base.sql.gz" \
  | sed -E 's/DEFINER=`[^`]+`@`[^`]+`//g; s/utf8mb4_uca1400_ai_ci/utf8mb4_unicode_ci/g' \
  | docker compose -f "$DIR/compose.mysql.yml" exec -T mysql sh -c \
    'export MYSQL_PWD="$(cat /run/secrets/mysql_root)"
     mysql -uroot -e "DROP DATABASE IF EXISTS \`$1\`; CREATE DATABASE \`$1\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
     mysql -uroot "$1"' _ "$BASE"

# 3. Archivos. static no se vacía: lo del código viene en la imagen y encima va
#    lo que subió el ISP.
echo "Cargando archivos…"
montajes=()
for volumen in wireguard static uploads whatsapp-uploads whatsapp-sesion estado; do
  docker volume create "${ISP}_$volumen" >/dev/null
  montajes+=(-v "${ISP}_$volumen:/datos/$volumen")
done
docker run --rm --network none "${montajes[@]}" -v "$TMP:/entrada:ro" --entrypoint sh mysql:8.4 -c '
  set -e
  for d in wireguard uploads whatsapp-uploads whatsapp-sesion estado; do find "/datos/$d" -mindepth 1 -delete; done
  tar xzf /entrada/volumenes.tar.gz -C /datos
  conf=/datos/wireguard/wg0.conf
  [ -s "$conf" ] || { echo "La exportación no trae /etc/wireguard/wg0.conf" >&2; exit 1; }
  # PostUp/PostDown son de la VPS vieja (iptables de su interfaz) y los Endpoint
  # son las IP de los MikroTik: con la misma llave, la nube les "robaría" el
  # túnel a la VPS vieja. Los MikroTik llegan solos cuando se les cambia el endpoint.
  sed -i -e "/^PostUp/d" -e "/^PostDown/d" -e "/^Endpoint/d" "$conf"
  chmod 700 /datos/wireguard && chmod 600 /datos/wireguard/*
  chown -R 1000:1000 /datos/whatsapp-uploads /datos/whatsapp-sesion'

# 4. Secretos que trae la VPS (SECRET_KEY y compañía) y la VPN como la tenía.
cp -a "$INST/api.env" "$INST/api.env.antes-de-importar"
RED="$(dato VPN_RED)"
{
  cat "$TMP/secretos.env"
  echo "VPN_SUBNET_BASE=${RED:-10.8.0}"
  echo "VPN_INTERFAZ_MIKROTIK=wg-fdeznet"
  echo "VPN_MARCA_VIGILANTE=fdeznet-vpn-watchdog"
} > "$TMP/poner.env"
if [[ "${PRUEBA:-0}" == "1" ]]; then
  sed -i '/^VAPID_/d' "$TMP/poner.env"  # que la copia no mande avisos a los celulares del ISP
fi
python3 - "$INST/api.env" "$TMP/poner.env" <<'PY'
import sys
destino, nuevos = sys.argv[1], sys.argv[2]
valores = dict(l.split("=", 1) for l in open(nuevos).read().splitlines() if "=" in l)
lineas = []
for linea in open(destino).read().splitlines():
    clave = linea.split("=", 1)[0]
    if clave in valores:
        linea = f"{clave}={valores.pop(clave)}"
    lineas.append(linea)
lineas += [f"{c}={v}" for c, v in valores.items()]
open(destino, "w").write("\n".join(lineas) + "\n")
PY
if [[ "$(dato WHATSAPP)" != "1" || "${PRUEBA:-0}" == "1" ]]; then
  echo "Sin sesión de WhatsApp: escanea el código desde el sistema en la nube."
fi

# 5. Encender: la API aplica las migraciones que falten.
"${compose[@]}" up -d
for _ in $(seq 1 100); do
  [[ "$(docker inspect -f '{{.State.Health.Status}}' "$ISP-api-1" 2>/dev/null)" == healthy ]] && break
  sleep 3
done
[[ "$(docker inspect -f '{{.State.Health.Status}}' "$ISP-api-1")" == healthy ]] || {
  echo "La API no arrancó; revisa: ${compose[*]} logs api" >&2; exit 1; }

# 6. Lo que se pega en cada MikroTik: el mismo peer, apuntando a la nube.
docker exec -i "$ISP-api-1" python - <<'PY'
import os, subprocess
from src.application.services.vpn_service import RED_VPN, construir_vigilante_mikrotik
publica = subprocess.check_output(["wg", "show", "wg0", "public-key"], text=True).strip()
ip, puerto = os.environ.get("VPN_SERVER_IP", ""), os.environ.get("WG_PORT", "")
print("\n# ===== Pegar en la terminal de CADA MikroTik del ISP =====")
print(f'/interface wireguard peers set [find public-key="{publica}"] endpoint-address={ip} endpoint-port={puerto}')
print(construir_vigilante_mikrotik(ip, int(puerto), f"{RED_VPN}.1"))
PY
echo "IMPORTADO=$ISP"
