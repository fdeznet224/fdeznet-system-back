# Validación de pagos por captura

Desde la 2.71.0 los pagos por transferencia se validan solo con la captura que
el cliente manda por WhatsApp. La integración con el correo del banco (Gmail)
se quitó: los correos de confirmación dejaron de llegar y solo llenaban el
sistema de registros sin usar. Una pasarela de pago (Mercado Pago, Stripe u
otra) se integrará después por separado.

## Cómo funciona

1. El agente de IA lee la captura (imagen o PDF) con OCR: monto, folio o clave
   de rastreo, fecha y hora, concepto y, en un CEP de Banxico, la cuenta
   beneficiaria.
2. Si el CEP es de una transferencia a una cuenta que no es del ISP, no se
   acepta.
3. Se aplica el pago solo (método `autovalidado`, registrado como "Agente IA")
   si:
   - el folio o código de la transferencia no se ha usado antes;
   - la fecha es reciente (máximo los días configurados) y no es futura;
   - el monto cubre la deuda y no es más del doble;
   - el cliente no tiene ya otro pago por captura en el mes y su chat no ha
     mandado demasiadas capturas en el día.
4. Si es de un mes siguiente, la captura es válida pero se aplica el día 1 del
   mes que cubre (tarea `tarea_aplicar_pagos_adelantados`).

## Lo que no se valida solo

Ya no hay bandeja de "Comprobantes por revisar". Si la captura no cumple, el
agente le dice al cliente que un asesor registra su pago, pausa el bot en ese
chat y avisa por WhatsApp a los teléfonos de alerta con el monto y el motivo.
El asesor registra el pago en la Terminal de Cobro; al hacerlo, la captura se
cierra sola y su folio queda bloqueado para que no se use otra vez.

## Configuración

En **Configuración → Integraciones → Pagos por captura**:

- terminaciones (4 dígitos) de las cuentas que reciben pagos;
- antigüedad máxima de la transferencia, en días.

Se guardan en la tabla `configuracion_correo_banco` (fila 1) para conservar lo
que ya estaba configurado. Las tablas del correo (`transacciones_correo_banco`
y los campos de Gmail) se dejaron en la base de datos sin uso.
