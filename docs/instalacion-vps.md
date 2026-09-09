# Instalación de una VPS de cliente

## VPS recomendada

- Ubuntu 24.04 LTS limpio, arquitectura x86_64/amd64.
- Recomendado: 4 vCPU, 8 GB de RAM y 80 GB SSD.
- Mínimo validado por el instalador: 4 GB de RAM y 30 GB libres.
- Acceso `root` por SSH.
- Un registro DNS tipo A apuntando el dominio a la IP pública de la VPS.
- Puertos TCP 22, 80 y 443, y UDP 51820 permitidos en el firewall del proveedor.
- Salida HTTPS y DNS hacia GitHub, PyPI, npm, NodeSource, Let's Encrypt,
  `api.ipify.org` y el servidor central `fdezpay.com`.
- No instalar previamente paneles como cPanel, Plesk o servicios que ocupen Nginx/MySQL.

Debian 12 también es compatible; el instalador selecciona automáticamente el
paquete MySQL/MariaDB disponible. Para nuevas ventas se recomienda Ubuntu 24.04
porque será la plataforma principal de pruebas.

## Preparación del dominio

1. Comprar o elegir un subdominio, por ejemplo `sistema.proveedor.com`.
2. Crear un registro DNS `A` con la IP pública de la VPS.
3. Esperar a que `dig +short sistema.proveedor.com` muestre esa misma IP.
4. No usar proxy de Cloudflare durante la instalación; se puede evaluar después.

## Flujo desde el panel central

1. Entrar a **Configuración → Licencias y versiones**.
2. Elegir **Nueva instalación** y capturar ISP, dominio y correo.
3. Copiar el comando generado. El token dura 48 horas y se consume una vez.
4. Conectarse a la nueva VPS por SSH y ejecutar el comando como `root`. Tendrá
   esta forma (el panel coloca los valores reales):

```bash
curl -fsSL https://fdezpay.com/api/control/installer | sudo bash -s -- \
  --domain sistema.proveedor.com \
  --email administrador@proveedor.com \
  --bootstrap-token TOKEN_DE_UN_SOLO_USO
```

Si todavía no existe un dominio, puede realizarse un piloto temporal por la IP
pública. En ese caso no se solicita certificado TLS:

```bash
curl -fsSL https://fdezpay.com/api/control/installer | sudo bash -s -- \
  --bootstrap-token TOKEN_DE_UN_SOLO_USO
```

El panel quedará disponible como `http://IP_DE_LA_VPS`. Al contar con el
dominio, cree el registro DNS A hacia esa IP y vuelva a ejecutar el instalador
con `--domain` y `--email`; la licencia existente se conserva y Certbot activa
HTTPS. El acceso por IP es apropiado para el piloto, pero no para operar datos
reales ni credenciales fuera de una red confiable. La instalación PWA, cámara,
geolocalización y otras funciones que exigen un contexto seguro deben probarse
después de activar el dominio con HTTPS.

La instalación suele tardar entre 10 y 25 minutos, principalmente por Chromium,
las dependencias de reconocimiento y la compilación del frontend. No se debe
cerrar la sesión SSH mientras trabaja.

El instalador valida el DNS antes de cambiar el servidor. Después instala MySQL,
WireGuard, backend, bot de WhatsApp, frontend, Nginx y el certificado HTTPS. Al
final imprime el usuario y la contraseña iniciales del administrador. La primera
vez que inicia la API también aplica automáticamente el nombre y correo del ISP
registrados en el panel central; después pueden personalizarse en **Marca blanca**.

También deja activos:

- `fdeznet-api`: API y tareas internas.
- `fdeznet-bot`: WhatsApp.
- `wg-quick@wg0`: túnel hacia routers.
- `fdeznet-backup.timer`: respaldo cifrado diario.
- `fdeznet-update.timer`: revisión de actualizaciones cada 30 minutos.
- `fdeznet-verify.timer`: prueba semanal de recuperación.

## Primer ingreso y entrega

1. Abrir la URL impresa por el instalador y entrar con las credenciales iniciales.
2. Cambiar la contraseña del administrador.
3. Configurar nombre, subir logotipo y favicon, elegir colores y completar los
   datos del ISP en **Marca blanca**.
4. Vincular WhatsApp y verificar que el bot quede conectado.
5. Registrar el primer router y comprobar el túnel WireGuard.
6. Ejecutar **Respaldar** y luego **Probar recuperación** desde el panel.
7. Confirmar desde el servidor central que la licencia reporta versión y conexión.

La clave del respaldo queda solamente en `/etc/fdeznet/backup.key`, con permiso
`0600`. Debe entregarse al responsable de la VPS por un canal seguro si él será
quien administre las recuperaciones.

## Seguridad e idempotencia

- No elimina `/opt/fdeznet` cuando se vuelve a ejecutar.
- Los repositorios existentes solo avanzan mediante `merge --ff-only`.
- La API y el bot se ejecutan con el usuario restringido `fdeznet`.
- Los secretos quedan en archivos con permisos `0600`.
- La llave de licencia se obtiene mediante un token de un solo uso.
- Antes de solicitar el certificado se comprueba que el DNS corresponde a la VPS.

Si una ejecución se interrumpe después de canjear el token, las credenciales ya
quedan guardadas en `/opt/fdeznet/backend/.env`; se puede ejecutar nuevamente el
mismo comando para continuar sin consumir otro token.

## Diagnóstico

```bash
systemctl status fdeznet-api fdeznet-bot nginx mysql wg-quick@wg0
journalctl -u fdeznet-api -n 100 --no-pager
curl -fsS https://DOMINIO/api/health/ready
systemctl list-timers fdeznet-backup.timer fdeznet-update.timer fdeznet-verify.timer
```

La respuesta esperada del health check es `{"status":"ready"}` y todos los
servicios deben aparecer como `active`.

## Prueba de aceptación antes de entregar

1. Confirmar que la versión instalada coincide con la versión publicada.
2. Volver a ejecutar el mismo instalador y comprobar que no cambia credenciales
   ni elimina información.
3. Crear un cliente de prueba, una factura y un pago con recibo PDF.
4. Enviar y recibir un mensaje de WhatsApp, incluida una imagen.
5. Subir logo y favicon y comprobar login, panel y PWA.
6. Suspender temporalmente la licencia desde el panel central y confirmar que
   la operación queda bloqueada; reactivarla y verificar la recuperación.
7. Crear, verificar y restaurar un respaldo de prueba.
8. Reiniciar la VPS y confirmar nuevamente servicios, temporizadores y health.

No se debe entregar la instalación mientras cualquiera de estas pruebas falle.
