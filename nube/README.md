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
| `respaldar-instancia.sh` | Respaldo de un ISP: base, volúmenes y secretos en un `.tar` |
| `restaurar-instancia.sh` | Regresa un ISP a un respaldo (antes respalda el estado actual) |

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

## Respaldos

El panel respalda a cada ISP todos los días a `PANEL_HORA_RESPALDO` (03:00) y
también con **Respaldar ahora** en su detalle. Cada respaldo es un solo archivo
`respaldos/<isp>/<isp>-AAAAMMDD-HHMMSS.tar` con:

- `base.sql.gz`: la base del ISP (`mysqldump --single-transaction`, sin detener nada).
- `volumenes.tar.gz`: WireGuard (llaves y peers), archivos subidos, logos,
  recibos y la sesión de WhatsApp (sin cachés de Chrome).
- `compose.env`, `api.env`: sus secretos, para llevarlo a otra VPS.

En la VPS se quedan los últimos `PANEL_RESPALDOS_LOCALES` (7). Para que haya
copia fuera de la VPS, configura un remoto de rclone en `secretos/rclone.conf`
y ponlo en `PANEL_RESPALDO_REMOTO`; allá se guardan `PANEL_RESPALDO_DIAS_REMOTO`
días (30). Como llevan secretos, usa un remoto cifrado (`crypt`), por ejemplo
sobre Cloudflare R2:

```bash
docker run --rm -it -v $PWD/secretos:/config/rclone rclone/rclone:1.68.2 config
# 1) remoto "r2" tipo s3 / Cloudflare con su access key
# 2) remoto "r2-cifrado" tipo crypt sobre "r2:fdezpay-respaldos" (guarda las claves aparte)
# En secretos/panel.env:  PANEL_RESPALDO_REMOTO=r2-cifrado:
docker compose -f compose.panel.yml --env-file secretos/panel.env up -d
```

**Restaurar** (detalle del ISP → respaldo → escribir su subdominio) detiene sus
contenedores, vacía su base y volúmenes, los carga del respaldo y lo vuelve a
encender. Antes deja un respaldo del estado actual. A mano:
`./restaurar-instancia.sh <isp> <archivo.tar>` (`ARRANCAR=0` lo deja detenido).
