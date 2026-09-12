# Sitio público de FdezNet

Sitio estático para `https://dev.fdezpay.com`. No forma parte del frontend de las instalaciones de clientes ni del panel central.

## Publicación

1. Copiar el contenido de esta carpeta, excepto `deploy`, a `/var/www/fdezpay-marketing`.
2. Instalar `deploy/dev.fdezpay.com.nginx` en `/etc/nginx/sites-available/dev.fdezpay.com` y habilitarlo.
3. Validar con `nginx -t` y recargar Nginx.
4. Emitir el certificado de `dev.fdezpay.com` con Certbot y verificar la redirección HTTPS.

Los precios y la fecha de la comparativa deben revisarse antes de cada cambio comercial.
