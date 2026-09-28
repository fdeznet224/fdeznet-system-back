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
