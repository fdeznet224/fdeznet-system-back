# Instalación de una VPS de cliente

## Requisitos

- Ubuntu 22.04/24.04 o Debian 12 limpio.
- Acceso `root` por SSH.
- Un registro DNS tipo A apuntando el dominio a la IP pública de la VPS.
- Puertos 80, 443 y UDP 51820 permitidos por el proveedor.

## Flujo desde el panel central

1. Entrar a **Configuración → Licencias y versiones**.
2. Elegir **Nueva instalación** y capturar ISP, dominio y correo.
3. Copiar el comando generado. El token dura 48 horas y se consume una vez.
4. Conectarse a la nueva VPS por SSH y ejecutar el comando como `root`.

El instalador valida el DNS antes de cambiar el servidor. Después instala MySQL,
WireGuard, backend, bot de WhatsApp, frontend, Nginx y el certificado HTTPS. Al
final imprime el usuario y la contraseña iniciales del administrador.

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
```
