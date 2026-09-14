# Versión 2.18.2 — Corrección del inicio del instalador

Fecha de liberación: 14 de septiembre de 2026.

## Corrección

- Corrige la salida prematura del instalador cuando el comando ya incluye
  `--domain` o `--ip`.
- Evita el mensaje secundario `curl: (23) Failure writing output to
  destination` provocado por el cierre anticipado de la tubería.
- Conserva el asistente interactivo cuando no se proporciona un modo de acceso.

Esta versión incluye también todas las correcciones de `2.18.1` para WebSocket,
datos FTTH y lectura de clientes con información histórica.
