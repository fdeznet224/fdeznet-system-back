# Versión 2.18.1 — Correcciones de WebSocket y datos FTTH

Fecha de liberación: 14 de septiembre de 2026.

## Correcciones

- Restaura la conexión WebSocket de WhatsApp detrás de Nginx al asegurar que
  los encabezados `Upgrade` y `Connection` se configuren dentro de `/api/`.
- Evita que la asignación o sustitución de una ONU copie su serial al campo de
  MAC WAN/CPE del cliente o del servicio.
- Permite consultar clientes con datos históricos sin convertir un valor MAC
  heredado en un error HTTP 500; las entradas nuevas y ediciones continúan
  exigiendo una MAC válida.
- Agrega al instalador un asistente inicial para elegir dominio con HTTPS o
  acceso temporal por IP, conservando argumentos para ejecuciones automáticas.

## Validación

- Suite completa del backend aprobada.
- Sintaxis del instalador y del agente de mantenimiento validada.
- WebSocket verificado en producción con conexión autenticada aceptada.
- API, Nginx y serialización del detalle de cliente verificados en producción.

## Despliegue

Esta versión no agrega migraciones de base de datos. El actualizador conserva
el respaldo previo, instala los commits fijados, recompila el frontend y valida
la salud del servicio antes de reportar el resultado.
