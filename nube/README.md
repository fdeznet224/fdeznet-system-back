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

## Primer arranque en una VPS

```bash
cd nube
install -d -m 0700 secretos
openssl rand -base64 32 > secretos/mysql_root && chmod 600 secretos/mysql_root
docker compose -f compose.mysql.yml up -d
./crear-instancia.sh prueba 18001 51821 prueba.fdezpay.com
```

`instancias/` y `secretos/` nunca se suben a git.

## Operación de un ISP

```bash
ISP=prueba
docker compose -p $ISP --env-file instancias/$ISP/compose.env -f compose.isp.yml ps
docker compose -p $ISP --env-file instancias/$ISP/compose.env -f compose.isp.yml logs -f api
docker compose -p $ISP --env-file instancias/$ISP/compose.env -f compose.isp.yml stop   # suspender
```

Los respaldos, la actualización y la verificación que en la VPS hacían los
timers de systemd los hará el panel central (Fase 4).
