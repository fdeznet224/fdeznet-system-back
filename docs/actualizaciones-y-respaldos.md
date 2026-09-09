# Actualizaciones y respaldos seguros

Cada instalación cliente revisa el servidor central cada 30 minutos. Solo instala una versión cuando el administrador publica los commits exactos de backend y frontend, los asigna a esa instalación y activa la actualización automática.

Antes de modificar código o base de datos, el agente crea un respaldo cifrado y verifica que pueda leerse. Incluye la base de datos, configuración, recibos/archivos estáticos, WireGuard y la sesión/archivos de WhatsApp. La retención local predeterminada es de 14 días.

El flujo valida la firma HMAC del manifiesto, el formato de los commits, que ambos cambios sean avances del historial y que el backend declare la versión esperada. Después instala dependencias, ejecuta migraciones, compila el frontend y revisa la salud local y pública. Si cualquier paso falla, restaura automáticamente código, base de datos, configuración y archivos desde el respaldo previo.

## Operación

- Estado: `GET /api/configuracion/mantenimiento`
- Respaldo manual: `POST /api/configuracion/mantenimiento/respaldo`
- Revisión manual: `POST /api/configuracion/mantenimiento/actualizar`
- Respaldo diario: `fdeznet-backup.timer`, alrededor de las 03:20.
- Revisión de versiones: `fdeznet-update.timer`, cada 30 minutos.

Los archivos quedan en `/var/backups/fdeznet`. La clave local está en `/etc/fdeznet/backup.key` con permisos exclusivos de root. Para protegerse contra pérdida completa de la VPS, se puede montar almacenamiento remoto y definir `FDEZNET_BACKUP_REMOTE_DIR` en `.env`; el agente copiará allí cada respaldo ya cifrado y su suma SHA-256. La clave debe resguardarse por separado en un gestor de secretos: perder la VPS y esa clave volvería irrecuperable el respaldo remoto.
