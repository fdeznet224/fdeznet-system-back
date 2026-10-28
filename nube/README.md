# FdezPay en la nube

Una instancia aislada por ISP: su propia base en el MySQL compartido, sus
contenedores (api, bot, web), sus volúmenes y su propio WireGuard.

| Archivo | Para qué |
|---|---|
| `api.Dockerfile` | API + jobs + WireGuard del ISP (sin sudo, con `NET_ADMIN`) |
| `bot.Dockerfile` | Bot de WhatsApp con el Chrome de puppeteer |
| `../../frontend-nube/Dockerfile` | Panel web; una compilación para todos (llama a `/api`) |
| `compose.mysql.yml` | MySQL compartido de la VPS (una vez por VPS) |
| `compose.isp.yml` | Contenedores de un ISP |
| `crear-instancia.sh` | Alta de un ISP: base, usuario, secretos y contenedores |
| `construir-version.sh` | Construye las imágenes de una versión (etiqueta = `src/version.py`) |
| `panel/`, `panel.Dockerfile`, `compose.panel.yml` | Panel central del dueño: ISP, mensualidades, suspender, actualizar |
| `preparar-panel.sh` | Crea `secretos/panel.env` con el usuario y la contraseña del panel |

## Primer arranque en una VPS

Las carpetas `backend-nube` y `frontend-nube` (rama `main-nube`) van juntas,
por ejemplo en `/opt/fdezpay/`.

```bash
cd /opt/fdezpay/backend-nube/nube
install -d -m 0700 secretos
openssl rand -base64 32 | tr -dc A-Za-z0-9 > secretos/mysql_root && chmod 600 secretos/mysql_root
docker compose -f compose.mysql.yml up -d
./construir-version.sh
docker compose -f compose.panel.yml --env-file /dev/null build
./preparar-panel.sh tudominio.com
docker compose -f compose.panel.yml --env-file secretos/panel.env up -d
```

Los ISP se dan de alta desde el panel (127.0.0.1:18000, y `panel.tudominio.com`
cuando esté el proxy de la Fase 3). `crear-instancia.sh` también sirve a mano.

`instancias/` y `secretos/` nunca se suben a git.

## Operación de un ISP

```bash
ISP=prueba
docker compose -p $ISP --env-file instancias/$ISP/compose.env -f compose.isp.yml ps
docker compose -p $ISP --env-file instancias/$ISP/compose.env -f compose.isp.yml logs -f api
docker compose -p $ISP --env-file instancias/$ISP/compose.env -f compose.isp.yml stop   # suspender
```

Las licencias ya no existen en la nube: el panel suspende al ISP deteniendo sus
contenedores y fija sus límites (`FDEZNET_LIMITE_CLIENTES`, `FDEZNET_LIMITE_ROUTERS`).
Los respaldos por ISP los hará el panel central (pendiente).
