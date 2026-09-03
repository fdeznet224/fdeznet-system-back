# Finanzas operativas y cobranza

El módulo registra dinero con `DECIMAL`, conserva el saldo anterior y posterior
de cada pago y separa el importe aplicado, el crédito generado y el crédito
utilizado.

## Flujo de caja

1. El cajero abre su caja en `POST /finanzas/caja/abrir`.
2. Los cobros en efectivo exigen una caja abierta. Transferencias y pagos
   autovalidados pueden recibirse sin efectivo físico.
3. Los gastos e ingresos operativos se registran en
   `POST /finanzas/caja/movimientos`.
4. El cierre compara apertura, entradas y salidas de efectivo contra el dinero
   entregado. La diferencia queda guardada.

Una caja cerrada no puede modificarse. Los movimientos independientes pueden
anularse por un administrador o supervisor mientras la caja siga abierta.

## Pagos, descuentos y anulaciones

- `POST /finanzas/cobrar` admite abonos, pagos exactos y excedentes.
- `clave_idempotencia` evita registrar dos veces el mismo cobro.
- `POST /finanzas/facturas/{id}/aplicar-saldo-favor` consume crédito existente.
- `POST /finanzas/facturas/{id}/descuento` requiere administrador o supervisor
  y guarda motivo y autorización.
- `POST /finanzas/pagos/{id}/anular` revierte únicamente el último pago aplicado.
  No permite alterar pagos de cajas cerradas ni crédito ya utilizado.
- `POST /finanzas/facturas/{id}/anular` anula una factura sin borrar su
  historial. Si tiene pagos aplicados, estos deben anularse primero. Para
  cambiar el ciclo, se puede enviar `nueva_fecha_facturacion` después de
  actualizar el día de pago de la plantilla.
- `POST /finanzas/factura-manual` consolida el cargo en la mensualidad abierta
  del mismo servicio siempre que todavía no tenga pagos aplicados; si no hay
  una mensualidad segura para modificar, crea una factura de cargo separada.
- `POST /finanzas/facturas/{id}/cotizar-reactivacion` recalcula una factura
  prepago usando los días reales con servicio. Los intervalos suspendidos no
  se cobran; una reactivación por promesa vuelve a contar como servicio desde
  ese día y un incumplimiento abre un nuevo intervalo de suspensión.
- Las mensualidades no se trasladan al ciclo siguiente: el cliente prepago
  debe liquidar la factura recalculada antes de reactivarse. La próxima
  mensualidad conserva su precio normal; únicamente los cargos adicionales
  del mismo ciclo se suman a su factura abierta.
- La fecha de pago real nunca cambia el ciclo contratado. En una plantilla de
  pago del 1 al 5 y corte el 6, una reactivación el día 8 cobra los días con
  servicio del 1 al 5 y del 8 hasta fin de mes; los días completos 6 y 7 no se
  cobran. El siguiente periodo vuelve a vencer el día 1, no el día 8.
- Las instalaciones nuevas no regalan un mes por defecto. Una activación el
  día 3 con ciclo calendario genera un primer prorrateo del 3 al último día del
  mes y deja el siguiente vencimiento en el día 1. Los meses gratis continúan
  disponibles como una elección explícita al activar el servicio.
- La descripción de la factura enumera los rangos con servicio y, cuando hay
  suspensión, los rangos sin servicio que fueron descontados. La notificación
  de una nueva factura admite `{detalle_cobro}`, `{dias_con_servicio}`,
  `{periodo_desde}` y `{periodo_hasta}`.

## Políticas y seguimiento

Cada cliente pertenece a una política de cobranza configurable: días máximos
de extensión, promesas simultáneas, incumplimientos permitidos en 90 días y
reconexión por promesa. Las promesas quedan como cumplidas o incumplidas en un
historial independiente.

`GET /finanzas/cobranza/pendientes-diarios` produce la ruta diaria de cobro y
`GET /finanzas/resumen-operativo` muestra ingresos, egresos, cartera y deuda
recuperada por periodo.

Las notificaciones automáticas se guardan en el historial de chat. Las claves
de evento evitan duplicados; `ack=-1` indica fallo, `0` pendiente, `1` enviado,
`2` entregado y `3` leído.
