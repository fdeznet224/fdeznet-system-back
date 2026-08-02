# Servicios por cliente y domicilio

## Modelo operativo

`ClienteModel` representa a la persona. `ServicioModel` representa cada
contrato o domicilio. Por ello:

- un cliente se cuenta una sola vez en el directorio;
- un cliente puede tener varios servicios;
- cada servicio tiene dirección, plan, router, red, IP, PPPoE, ONU, NAP,
  puerto, estado y calendario de facturación propios;
- cada factura, orden, baja, diagnóstico y lectura óptica debe guardar
  `servicio_id`;
- suspender, reactivar o cancelar un servicio no modifica los otros
  domicilios del cliente.

El estado general del cliente es derivado: activo si tiene al menos un
servicio activo; suspendido si no tiene activos pero sí suspendidos; pendiente
si solo tiene instalaciones pendientes; cancelado si no conserva servicios
vigentes.

## Flujo de API

1. La persona se registra una sola vez en `/clientes`.
2. Cada contrato se crea con `POST /servicios/`. El backend lo deja como
   `pendiente_instalacion` y puede crear su orden técnica.
3. La instalación se confirma con
   `POST /servicios/{servicio_id}/activar`. En este punto se asignan IP,
   PPPoE, ONU, NAP/puerto y fechas de cobro.
4. La facturación masiva genera una factura por cada servicio activo o
   suspendido. La restricción de periodo evita duplicar la mensualidad del
   mismo servicio.
5. El pago de una factura solo puede reactivar el servicio de esa factura y
   únicamente cuando ese mismo servicio no conserva otra deuda exigible.
6. Las órdenes, incidencias y bajas reciben `servicio_id`. Si un cliente tiene
   varios servicios y no se indica cuál, el backend rechaza la operación.
7. El diagnóstico por domicilio está disponible bajo
   `/network/diagnostico/servicios/{servicio_id}/...`.

Los endpoints antiguos por `cliente_id` se conservan para compatibilidad. Si
el cliente tiene más de un servicio, los cambios técnicos o administrativos
ambiguos se rechazan y se debe usar el flujo por `servicio_id`.

## Métricas

- `total_clientes`: personas únicas con al menos un servicio activo o
  suspendido; no incluye instalaciones pendientes.
- `total_servicios_actuales`: contratos activos más suspendidos.
- online/offline de red: sesiones por servicio, porque cada domicilio tiene
  su propia conexión.
- facturación: reporta por separado clientes facturados, servicios facturados
  y facturas emitidas.

## Despliegue

1. Crear un respaldo verificable de MySQL.
2. Detener temporalmente los procesos de facturación, cortes y sincronización.
3. Desplegar el código y ejecutar `./venv/bin/alembic upgrade head`.
4. Confirmar que Alembic reporta la revisión `c3d4e5f6a7b8`.
5. Iniciar la aplicación y reconciliar:
   clientes actuales, servicios actuales, facturas por servicio y sesiones
   MikroTik.
6. Actualizar el frontend para seleccionar el domicilio antes de crear una
   factura manual, incidencia, orden o baja.

La migración toma el servicio de menor ID como contrato principal de cada
cliente y copia allí la configuración técnica histórica. Esto evita duplicar
IP, ONU o puerto si existen registros de servicio adicionales anteriores.
