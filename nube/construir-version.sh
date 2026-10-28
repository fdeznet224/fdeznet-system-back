#!/usr/bin/env bash
# Construye las imágenes de una versión para aplicarla desde el panel.
#   nube/construir-version.sh            (usa src/version.py)
set -Eeuo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
VERSION="${1:-$(sed -n 's/^SYSTEM_VERSION *= *"\(.*\)"/\1/p' "$DIR/../src/version.py")}"
[[ -n "$VERSION" ]] || { echo "No se encontró la versión" >&2; exit 1; }
# compose usa BuildKit; los puertos y el secreto son de relleno, solo se construye.
printf 'ISP=build\nWEB_PORT=1\nWG_PORT=1\nWEBHOOK_SECRET=x\nIMAGEN_VERSION=%s\n' "$VERSION" > "$DIR/.construir.env"
trap 'rm -f "$DIR/.construir.env"' EXIT
docker compose -p construir --env-file "$DIR/.construir.env" -f "$DIR/compose.isp.yml" \
  --profile whatsapp build
echo "Versión $VERSION lista; aplícala a cada ISP desde el panel."
