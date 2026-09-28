# Versión 2.19.0 — Cobranza: reconexión confiable y un solo cobro

Fecha de liberación: 27 de septiembre de 2026.

- Backend: `67a83f3b0664097658edaf45dcdaed434e72165a`
- Frontend: `d96d6e7b09c331e760f51c727bf2a23fe0a85949`

## Diagnóstico que originó el cambio

Clientes que ya habían pagado seguían suspendidos (caso de Florisela Alfaro,
factura #939, y Elizabeth Lopez, factura #943, ambas del router Paraiso):

- Al cortarlas el 26/09, el recálculo por días sin servicio bajó la factura de
  $350 a $338.33, pero el renglón de internet siguió en $350.
- Pagaron $338.33; la factura quedó `pagada`, pero el renglón conservó $11.67.
- La reconexión revisaba el renglón y no reconectaba.
- En producción había 8 facturas con ese descuadre (7 pagadas y la #956).

## Cambios

- **El saldo de la factura manda.** `cuadrar_conceptos` ajusta los renglones
  al recalcular días suspendidos, aplicar descuentos y anular facturas o pagos.
  Un verificador cada hora repara cualquier descuadre (`CuadreFacturas`).
- **Reconexión confiable.** Corte y reconexión usan el mismo criterio de deuda
  vencida. Cada 10 minutos se reconecta a quien ya no debe nada vencido, por
  ejemplo si el MikroTik no respondió al cobrar (`ReactivacionPagados`).
- **Un solo cobro por cliente.** Caja y cobrador ven un único total
  (mensualidad, prorrateo, extras y reconexión) y lo cobran de una vez; un solo
  WhatsApp con un solo recibo.
- **Reconexión en la misma factura.** Al cortar, el cargo de reconexión de la
  plantilla se suma una vez a la factura del corte y se paga en el mismo cobro.
- **Baja automática** tras 90 días suspendido por adeudo (configurable, 0 la
  desactiva).
- **Modo suspendido "solo WhatsApp"** (desactivado por defecto): el moroso
  conserva WhatsApp a velocidad limitada. Requiere prueba en un router antes de
  activarlo.

Reglas completas: `docs/finanzas-cobranza.md`, sección "Reglas de cobranza".

## Pruebas

- 323 pruebas de backend aprobadas.
- Frontend: sin errores nuevos de TypeScript ni de lint.
- No hubo prueba visual de las pantallas de cobro antes de liberar.

## Despliegue en la central

1. Revisión previa (solo lectura): 8 facturas a cuadrar, todas con remanente
   a la baja; 2 servicios a reconectar; ninguna baja automática pendiente.
2. Respaldo cifrado verificado:
   `/var/backups/fdeznet/20260927-225836.tar.gz.gpg`.
3. `git reset --hard` a los commits de arriba, `alembic upgrade head`
   (migraciones `b3c4d5e6f7a9` y `c4d5e6f7a8b0`) y `npm run build` del
   frontend. Sin cambios en dependencias, bot ni archivos de despliegue.
4. Reinicio de `fdeznet-api`; `/api/health/ready` responde 200 local y
   públicamente. Las tareas nuevas quedaron programadas.
5. `FDEZNET_RELEASE_VERSION`, `FDEZNET_BACKEND_COMMIT` y
   `FDEZNET_FRONTEND_COMMIT` actualizados en `.env`.

## Pendientes operativos

- Registrar la versión 2.19.0 en **Licencias y versiones** si debe asignarse a
  instalaciones de clientes.
- Florisela y Elizabeth se reconectan solas cuando Paraiso vuelva a estar en
  línea (al liberar estaban caídos Paraiso, Vicente Guerrero y Flores Magon).
- Los 18 clientes suspendidos antes de esta versión no tienen el cargo de
  reconexión en su factura; al pagar no se les cobrará.
- Probar el modo "solo WhatsApp" en un router antes de activarlo.

## Versión 2.19.1 (27 de septiembre de 2026)

- Backend: `54904cd60e04a2ea8a8b589e62cdfbf960abccc0`
- Frontend: `3b5eea1880a4c65e656e0b1470175caf0601a964`

Cambios:

- La pantalla de cobro lista en palabras lo que se cobra ("Mensualidad de
  julio 2026", "Servicio extra: …", "Cargo por reconexión") y resalta la
  mensualidad actual. El recuadro del monto se llama "Total a pagar" y el
  importe ya no se repite.
- La lista del cobrador mostraba el saldo sin descontar los días suspendidos
  (Claudia Vazquez: $861.29) y al abrir al cliente se recalculaba ($601.29).
  La tarea horaria de cuadre ahora recalcula primero a los suspendidos al día
  de hoy.

Despliegue: mismo procedimiento, sin migraciones ni cambios de dependencias,
con el respaldo `20260927-225836` de la 2.19.0. Resultado de la primera
pasada (23:26): 7 facturas cuadradas (#921, #927, #933, #939, #943, #980,
#983), la #956 quedó cuadrada al recalcularse, 0 facturas descuadradas y
Claudia Vazquez aparece con $601.29 en la lista. Florisela (servicio 19) y
Elizabeth (servicio 23) siguen pendientes de reconexión porque Paraiso no
responde; se reintenta cada 10 minutos.

## Versión 2.19.2 (27 de septiembre de 2026)

- Backend: `51164a8c9480c69e33f708879e8112e9bec19cef` (frontend sin cambios)

El "mes actual" de la pantalla de cobro es ahora la mensualidad más reciente
generada, no la que contiene la fecha de hoy. Claudia Vazquez resalta octubre
(#1046) en lugar de septiembre; Claudia Lopez, con solo octubre (#999), ahora
también lo ve resaltado. Despliegue solo de backend, sin migraciones.

## Versión 2.19.3 (28 de septiembre de 2026)

- Backend: `5df19643168da05c84596278b2bcd67995aa8db2`
- Frontend: `64892fdf19df926322619d5a158c5570f7740598`

Cada línea de "Se está cobrando" muestra su monto (mensualidad, reconexión,
servicio extra) para explicar el total al cliente.

## Versión 2.20.0 (28 de septiembre de 2026)

- Backend: `f7b04ec78cd1e0561c0bd0ba319e4e5fcffc1a75`
- Frontend: `856b920b7caa3c79fd9d6cb6d6816708c143c1da`

El bot se calla 2 horas en un chat cuando responde un asesor, desde el panel o
desde el celular; nueva acción "Hablar con un asesor" en el flujo visual.
Detalle en `docs/bot-whatsapp-configurable.md`. Migración `d5e6f7a8b9c1`
(tabla `bot_pausas`) y reinicio de `fdeznet-bot`, que se reconectó sin pedir
QR.

## Versión 2.20.1 (28 de septiembre de 2026)

- Backend: `8ad18b9fdb4511b01b2b255d79411498855f630d` (frontend sin cambios)

Un solo bot de clientes: el flujo visual (comando `fdezbot`, también `menu` y
`fdezpay`). Se retiró el menú numerado anterior. Al liberar, el flujo de
clientes estaba activo y `inicio_fuera_horario` estaba desactivado, por lo que
el bot no contesta solo fuera de horario hasta que se active en la
configuración.
