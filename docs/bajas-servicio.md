# Bajas de servicio

## Flujo canónico

La baja se administra mediante `/bajas` y mantiene un expediente independiente
del cliente. El flujo conserva la deuda, detiene la facturación futura, libera
el puerto NAP y genera una orden de retiro cuando existe una ONU asignada.

Estados esperados:

1. `pendiente_retiro`: el servicio fue cancelado y hay equipo por recuperar.
2. `sin_equipo`: el cliente no tenía una ONU asociada.
3. `equipo_recuperado`: el retiro fue confirmado y el inventario actualizado.
4. `cancelada`: la baja fue revertida y el servicio reactivado.

La condición informada al confirmar el retiro determina el destino de la ONU:

| Condición | Estado de inventario |
| --- | --- |
| `funcional` | `DISPONIBLE` |
| `danada` | `DANADA` |
| `incompleta` | `INCOMPLETA` |
| `perdida` | `PERDIDA` |

## Endpoints vigentes

- `POST /bajas/clientes/{cliente_id}`: inicia la baja.
- `GET /bajas/`: consulta expedientes.
- `POST /bajas/{baja_id}/asignar`: asigna el retiro.
- `POST /bajas/{baja_id}/confirmar-retiro`: confirma la recuperación.
- `POST /bajas/ordenes/{orden_id}/confirmar-retiro`: confirma desde una orden.
- `POST /bajas/{baja_id}/reintentar-mikrotik`: reintenta la desactivación.
- `POST /bajas/{baja_id}/cancelar-reactivar`: revierte una baja abierta.

## Compatibilidad temporal

Las siguientes operaciones siguen funcionando para no romper clientes
anteriores, pero aparecen como `deprecated` en OpenAPI:

- `POST /clientes/{cliente_id}/dar-de-baja`
- `POST /clientes/{cliente_id}/reactivar`
- `POST /clientes/inventario/{inventario_id}/confirmar-retiro-onu`
- `POST /clientes/inventario/{inventario_id}/asignar-retiro/{tecnico_id}`

Antes de retirarlas se debe comprobar en los registros del proxy o de la API que
ya no reciben tráfico durante una ventana completa de operación.

