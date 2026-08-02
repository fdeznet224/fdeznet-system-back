# Órdenes técnicas y control FTTH

Todas las rutas requieren token Bearer.

## Flujo de una orden

Estados permitidos:

```text
pendiente -> asignada -> en_camino -> trabajando -> terminada
     \           \             \          \
      +-----------+-------------+-----------> cancelada
```

Las actualizaciones de estado reciben `version`. Si otro dispositivo modificó
la orden, la API responde `409` para evitar sobrescribir trabajo sin
sincronizar.

### Rutas principales

- `GET /ordenes/`: lista órdenes; un técnico solo recibe las suyas.
- `POST /ordenes/`: crea instalación, reparación, cambio de domicilio,
  cambio de ONU o retiro.
- `PATCH /ordenes/{id}`: programa, asigna técnico y registra
  diagnóstico/solución.
- `POST /ordenes/{id}/estado`: avanza o cancela la orden.
- `POST /ordenes/{id}/materiales`: registra material utilizado.
- `POST /ordenes/{id}/evidencias`: sube foto, firma o PDF, máximo 10 MB.
- `GET /ordenes/{id}/evidencias/{evidencia_id}`: descarga autenticada.
- `POST /ordenes/{id}/completar-instalacion`: ejecuta el cierre guiado,
  asigna ONU/puerto, registra potencia, activa MikroTik y cierra la orden.

Para terminar cualquier orden se requiere una solución y al menos una
evidencia. Las instalaciones también requieren `conformidad_cliente=true`.

## Control FTTH

- `GET /ftth/naps/{nap_id}/puertos`: matriz de puertos y ocupación.
- `PATCH /ftth/naps/{nap_id}/puertos/{numero}`: libre, reservado o dañado.
- `POST /ftth/naps/{nap_id}/puertos/{numero}/asignar`: asignación exclusiva.
- `POST /ftth/clientes/{cliente_id}/lecturas-opticas`: guarda RX/TX.
- `GET /ftth/clientes/{cliente_id}/lecturas-opticas`: historial de potencia.
- `GET /ftth/onus/{onu_id}/historial`: movimientos y condiciones de la ONU.

La base de datos impide que una ONU o un puerto NAP se asignen a más de un
cliente. Dar de baja una ONU conserva su historial en lugar de eliminarla.

## Automatizaciones existentes

- Registrar un cliente crea su orden de instalación.
- Dar de baja cancela facturación futura, libera el puerto y crea una orden
  para recuperar la ONU.
- Confirmar el retiro devuelve la ONU a bodega y termina la orden.
- Cambiar una ONU registra tanto la salida del equipo anterior como la
  instalación del nuevo.
