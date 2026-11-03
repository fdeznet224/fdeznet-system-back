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
| `compose.proxy.yml`, `proxy/` | Caddy: HTTPS del panel y de cada ISP en su subdominio |
| `respaldar-instancia.sh` | Respaldo de un ISP: base, volúmenes y secretos en un `.tar` |
| `restaurar-instancia.sh` | Regresa un ISP a un respaldo (antes respalda el estado actual) |

## Primer arranque en una VPS

Las carpetas `backend-nube` y `frontend-nube` (rama `main-nube`) van juntas,
por ejemplo en `/opt/fdezpay/`.

```bash
cd /opt/fdezpay/backend-nube/nube
install -d -m 0700 secretos
openssl rand -base64 32 | tr -dc A-Za-z0-9 > secretos/mysql_root && chmod 600 secretos/mysql_root
docker network create fdezpay-proxy
docker compose -f compose.mysql.yml up -d
./construir-version.sh
docker compose -f compose.panel.yml --env-file /dev/null build
./preparar-panel.sh tudominio.com
docker compose -f compose.panel.yml --env-file secretos/panel.env up -d
echo DOMINIO=tudominio.com > proxy/.env
docker compose -f compose.proxy.yml --env-file proxy/.env up -d
```

En el DNS del dominio: registros A de `@` y `*` hacia la IP de la VPS. El panel
queda en `panel.tudominio.com` y cada ISP en `<isp>.tudominio.com`. Caddy saca
el certificado de cada ISP la primera vez que alguien entra, solo si el panel
confirma que existe (`/caddy/permitir`). Si un ISP está suspendido, su
dirección muestra "Servicio no disponible".

Los ISP se dan de alta desde el panel (`panel.tudominio.com`, o 127.0.0.1:18000 por túnel SSH). `crear-instancia.sh` también sirve a mano.

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

**Corte automático.** Si un ISP no paga, al terminar `PANEL_DIAS_GRACIA` (5) días
después de su "pagado hasta" el panel lo suspende solo, a partir de
`PANEL_HORA_CORTE` (09:00). Al registrar su pago se reactiva sin hacer nada más.
Si lo reactivas a mano mientras todavía debe, pide una prórroga (por defecto
`PANEL_DIAS_PRORROGA`, 3 días) y se vuelve a suspender si no paga antes. El corte
se apaga por ISP en *Datos del ISP → Corte automático*; los ISP sin pagos
registrados nunca se cortan solos.

**Aviso al ISP.** Desde `PANEL_DIAS_AVISO` (5) días antes de vencer, el
administrador del ISP ve arriba en su sistema una franja ámbar con la fecha de
vencimiento y la del corte; ya vencido, la franja es roja. Si pones
`PANEL_CONTACTO_PAGO` (por ejemplo «WhatsApp 55 1234 5678») se agrega al aviso.
La API del ISP lo consulta firmado en `/cuenta/estado` y lo guarda 10 minutos.

## Mudar a la nube un ISP que tenía su propia VPS

Para un ISP instalado con `install.sh`. Se conservan sus datos, sus usuarios y
contraseñas, su marca y su VPN: la llave del servidor WireGuard, su red
(10.8.0.x) y el `SECRET_KEY` que cifra las contraseñas de routers y OLT. En sus
MikroTik solo cambia a dónde apunta el túnel. El sistema queda apagado mientras
dura la mudanza (unos minutos).

1. **Panel → Nuevo ISP** con su subdominio, precio, versión y bot. La versión
   tiene que ser igual o más nueva que la de su VPS.
2. **En su VPS** (como root): copia `nube/exportar-vps.sh` y córrelo. Detiene
   su sistema y deja `/root/wispay-exportacion-….tar`.
   `--sin-whatsapp` si prefieres escanear el código otra vez en la nube.
3. Pasa el archivo a `nube/migraciones/` en la VPS de nube (nunca se sube a git) y:
   ```bash
   nube/importar-vps.sh <isp> migraciones/wispay-exportacion-….tar
   ```
   Respalda la instancia vacía, carga todo, la enciende e imprime lo que hay
   que **pegar en la terminal de cada MikroTik** (el endpoint nuevo y el
   vigilante de la VPN).
4. Comprueba en `https://<isp>.<dominio>` que entra, que sus routers aparecen en
   línea y que WhatsApp quedó vinculado.
5. **En su VPS**: `exportar-vps.sh --retirar <isp>.<dominio>` apaga para
   siempre su sistema (sin borrar datos) y manda su dirección vieja a la nueva.

Si algo sale mal antes del paso 5: `exportar-vps.sh --regresar` en su VPS lo
vuelve a encender, y en los MikroTik se regresa el endpoint a la IP de su VPS.

Para ensayar con una copia sin apagar nada: `exportar-vps.sh --sin-detener
--sin-whatsapp` y `PRUEBA=1 nube/importar-vps.sh …` en una instancia de
ensayo. La copia no puede tomar los túneles: se le quitan las direcciones de
los MikroTik.

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

### En el Google Drive de cada ISP

Cada ISP puede conectar su propio Google Drive en *Configuración → Almacenamiento*.
Desde entonces su respaldo diario se sube **cifrado** a la carpeta
`PANEL_DRIVE_CARPETA` («Respaldos WisPay») de su Drive. Ahí se guardan
`PANEL_DRIVE_DIAS` días (30) y en la VPS solo queda el último
(`PANEL_RESPALDOS_LOCALES_CON_DRIVE=1`). El permiso que se pide es `drive.file`:
el sistema solo ve los archivos que él mismo sube, nada más del Drive del ISP.

1. En Google Cloud Console crea un proyecto. En *Pantalla de consentimiento
   de OAuth* pon el nombre de la app y el correo de soporte, agrega los permisos
   `openid`, `email` y `.../auth/drive.file`, y **publica la app** («En
   producción»). En modo de prueba Google vence la conexión a los 7 días.
2. Habilita la *Google Drive API*.
3. *Credenciales → Crear ID de cliente de OAuth → Aplicación web*, con URI de
   redireccionamiento `https://panel.<dominio>/drive/regreso`.
4. En `secretos/panel.env`:
   ```
   PANEL_GOOGLE_CLIENT_ID=...apps.googleusercontent.com
   PANEL_GOOGLE_CLIENT_SECRET=...
   PANEL_RESPALDO_CLAVE=<openssl rand -base64 32>
   ```
   **Guarda `PANEL_RESPALDO_CLAVE` también fuera de la VPS**: sin ella los
   respaldos de Drive no se pueden abrir.

La llave de Google de cada ISP queda en `secretos/drive/<isp>.conf` (remoto de
rclone `cifrado:`). La API de cada ISP le pide las cosas al panel firmadas con
`NUBE_PANEL_SECRETO` (su `api.env`; el panel lo agrega al arrancar a los ISP
que no lo tengan, y la API lo toma al recrearse). Para restaurar desde Drive:
detalle del ISP → *En su Google Drive* → **Traer a la VPS**, y luego Restaurar.

**Restaurar** (detalle del ISP → respaldo → escribir su subdominio) detiene sus
contenedores, vacía su base y volúmenes, los carga del respaldo y lo vuelve a
encender. Antes deja un respaldo del estado actual. A mano:
`./restaurar-instancia.sh <isp> <archivo.tar>` (`ARRANCAR=0` lo deja detenido).
