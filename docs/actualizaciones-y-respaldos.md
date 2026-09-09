# Actualizaciones y respaldos seguros

Cada instalación cliente revisa el servidor central cada 30 minutos. Solo instala una versión cuando el administrador publica los commits exactos de backend y frontend y los asigna a esa instalación. Si la actualización automática está apagada, el administrador local debe solicitarla con **Revisar actualización**; esa autorización se consume una sola vez. Si está encendida, el temporizador puede instalarla sin intervención adicional.

Antes de modificar código o base de datos, el agente crea un respaldo cifrado y verifica que pueda leerse. Incluye la base de datos, configuración, recibos/archivos estáticos, WireGuard y la sesión/archivos de WhatsApp. La retención local predeterminada es de 14 días.

El flujo valida la firma HMAC del manifiesto, el formato de los commits, que ambos cambios sean avances del historial y que el backend declare la versión esperada. Después instala dependencias, ejecuta migraciones, compila el frontend y revisa la salud local y pública. Si cualquier paso falla, restaura automáticamente código, base de datos, configuración y archivos desde el respaldo previo.

## Operación

- Estado: `GET /api/configuracion/mantenimiento`
- Respaldo manual: `POST /api/configuracion/mantenimiento/respaldo`
- Solicitud manual del usuario: `POST /api/configuracion/mantenimiento/actualizar`
- Prueba manual de recuperación: `POST /api/configuracion/mantenimiento/verificar`
- Respaldo diario: `fdeznet-backup.timer`, alrededor de las 03:20.
- Revisión de versiones: `fdeznet-update.timer`, cada 30 minutos.
- Auditoría no destructiva: `fdeznet-verify.timer`, semanalmente.

Los archivos quedan localmente en `/var/backups/fdeznet`. La clave está en `/etc/fdeznet/backup.key` con permisos exclusivos de root. La auditoría semanal comprueba la suma SHA-256, descifra el respaldo, valida su estructura, revisa el dump comprimido de MySQL y confirma los commits guardados sin reemplazar datos de producción.
