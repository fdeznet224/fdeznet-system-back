# Validación de pagos con Gmail

## Objetivo

El comprobante enviado al chatbot sirve para identificar al cliente, el monto y la referencia. El pago solamente puede aplicarse automáticamente cuando existe un correo bancario auténtico que coincide en referencia, monto y fecha.

La captura nunca es la fuente final de confirmación.

## Seguridad aplicada

- Gmail se consulta por IMAP en modo de solo lectura; los mensajes no se marcan como leídos ni se eliminan.
- Se usa una contraseña de aplicación de Google, nunca la contraseña normal de la cuenta.
- La contraseña queda cifrada con la `SECRET_KEY` de la instalación y nunca vuelve al navegador.
- Solo se aceptan remitentes configurados exactamente por el administrador.
- Para aprobación automática se exigen resultados DKIM y DMARC válidos, generados por Gmail y alineados con el dominio remitente.
- Cada `Message-ID`, referencia bancaria, transacción y clave de pago se controla contra duplicados.
- No se guarda el cuerpo completo del correo; solo importe, referencia, fecha, remitente, asunto, autenticación y hash para auditoría.
- Si falta un dato, hay más de una coincidencia o el correo no está autenticado, el caso permanece en revisión manual.

## Configuración inicial

1. En la cuenta Gmail que recibe los avisos del banco, activar la verificación en dos pasos.
2. Crear una contraseña de aplicación de Google para FdezNet.
3. Abrir un aviso real de Banco Azteca y copiar exactamente la dirección del campo `De`.
4. En **WhatsApp > Comprobantes > Validación por correo bancario**, capturar:
   - cuenta Gmail;
   - contraseña de aplicación de 16 caracteres;
   - remitente exacto del banco;
   - texto del asunto, si se desea limitar aún más la búsqueda.
5. Mantener la validación desactivada y guardar.
6. Pulsar **Probar conexión**. Solo después de una prueba correcta se permite activarla.
7. Activar primero con **Aprobar automáticamente** apagado y sincronizar una transferencia controlada.
8. Confirmar que monto, referencia, fecha y DKIM/DMARC se muestran correctamente en la bandeja.
9. Después de esa prueba real, habilitar la aprobación automática.

Si se cambia la cuenta, la contraseña de aplicación o la contraseña principal de Google, hay que volver a guardar y probar la conexión.

## Flujo operativo

1. El cliente envía la captura por WhatsApp.
2. OCR propone monto, referencia y número de contrato.
3. El cliente confirma la cuenta a la que desea aplicar el pago.
4. El sistema sincroniza Gmail y busca una transacción disponible con la misma referencia y monto dentro de la ventana configurada.
5. Si hay una coincidencia autenticada y única:
   - con aprobación manual, queda marcada como correo confirmado;
   - con aprobación automática, se registra el pago, se concilia la transacción y se intenta reactivar el servicio en MikroTik.
6. Si no hay coincidencia inequívoca, no se registra dinero y el comprobante sigue en la bandeja.

## Diagnóstico

La pantalla muestra la fecha de la última revisión y el último error de Gmail. También puede ejecutarse **Sincronizar ahora**.

En servidor:

```bash
sudo systemctl status fdeznet-api --no-pager -l
sudo journalctl -u fdeznet-api -n 150 --no-pager
```

La tarea automática se ejecuta cada minuto. Si la integración está desactivada no realiza conexiones externas.
