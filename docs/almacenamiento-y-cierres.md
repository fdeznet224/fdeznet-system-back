# Almacenamiento y cierres

El módulo se encuentra en **Configuración > Almacenamiento** y está disponible
únicamente para administradores.

## Qué muestra

- Uso total y espacio libre del disco.
- Tamaño de comprobantes de WhatsApp, recibos PDF, evidencias de órdenes,
  archivos de marca, sesión de WhatsApp, respaldos cifrados y base de datos.
- Las tablas de base de datos que más espacio consumen.
- Cantidad de comprobantes pendientes, aprobados, rechazados, archivados y con
  imagen ya depurada.

## Cierre mensual

El cierre archiva los comprobantes aprobados y rechazados del periodo. Los
pendientes permanecen en la bandeja y pueden resolverse después. Cerrar no
elimina archivos inmediatamente: la eliminación depende de la antigüedad
configurada.

El cierre puede ejecutarse manualmente para un periodo `AAAA-MM` o
automáticamente el día configurado para el mes anterior.

## Limpieza segura

La tarea diaria puede retirar:

- imágenes de comprobantes archivados que ya cumplieron su retención;
- recibos PDF ya enviados que cumplieron su retención;
- archivos antiguos de WhatsApp que no están vinculados a un comprobante;
- respaldos antiguos durante el siguiente respaldo programado.

Nunca elimina registros de pagos, facturas, folios, hashes antifraude,
comprobantes pendientes, evidencias de órdenes, identidad de marca ni la sesión
activa de WhatsApp. Las terminaciones bancarias y demás credenciales tampoco se
incluyen en las métricas devueltas al navegador.
