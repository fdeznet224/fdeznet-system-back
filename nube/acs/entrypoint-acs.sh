#!/usr/bin/env bash
# Arranque del ACS de un ISP.
# 1. Las redes privadas (las de los clientes del ISP) se alcanzan por la API,
#    que tiene el WireGuard del ISP: así el ACS puede "llamar" a un módem al
#    momento (connection request). La API las enmascara al salir por el túnel.
# 2. Usuario/clave del ACS y la provisión de alta en su base.
# 3. cwmp y nbi; si uno se cae, se cae el contenedor y Docker lo reinicia.
set -Eeuo pipefail

REDES_PRIVADAS=(10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10)

rutas() {
  local api
  api="$(getent hosts "${ACS_API_HOST:-api}" | awk '{print $1; exit}')" || true
  [[ -n "$api" ]] || return 0
  # Las redes de Docker (172.x/16, más específicas) siguen saliendo directo.
  for red in "${REDES_PRIVADAS[@]}"; do
    ip route replace "$red" via "$api" 2>/dev/null || true
  done
}

rutas
# La API cambia de IP si se recrea (al actualizar): se revisa cada minuto.
( while sleep 60; do rutas; done ) &

node /opt/genieacs/preparar-acs.js

como_genieacs() { setpriv --reuid=genieacs --regid=genieacs --clear-groups "$@"; }
como_genieacs genieacs-cwmp &
como_genieacs genieacs-nbi &
wait -n
echo "[acs] un proceso de GenieACS terminó; se reinicia el contenedor" >&2
exit 1
