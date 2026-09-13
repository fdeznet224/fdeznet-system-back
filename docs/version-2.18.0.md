# Versión 2.18.0 — DHCP estático con control de velocidad

Fecha de liberación: 13 de septiembre de 2026.

## Objetivo

La versión 2.18.0 permite administrar clientes que no utilizan PPPoE y se
conectan mediante DHCP sobre redes de fibra óptica. Cada servicio puede quedar
asociado a una dirección IP y a la MAC WAN/CPE que MikroTik observa, aplicando
la velocidad del plan mediante el `rate-limit` del lease DHCP estático.

PPPoE continúa disponible y conserva su comportamiento anterior. El modo de
acceso se elige por nodo MikroTik.

## Configuración de nodos

- En **Infraestructura → Routers → Vincular nodo** se puede elegir:
  - **PPPoE (PPP Secrets)**.
  - **DHCP estático (IP + MAC)**.
- En PPPoE, el control continúa mediante perfiles PPP y colas dinámicas.
- En DHCP, el control se aplica mediante `rate-limit` en el lease estático, que
  genera la cola dinámica correspondiente en RouterOS.
- La interfaz explica que el servidor DHCP debe existir previamente en el
  MikroTik.
- Se muestra una advertencia para revisar FastTrack, debido a que una regla que
  omita el tráfico administrado puede impedir que las colas limiten la
  velocidad.
- No se permite cambiar entre PPPoE y DHCP cuando el nodo ya tiene servicios
  vigentes. En ese caso debe crearse otro nodo o realizar una migración técnica
  controlada.

## Alta e instalación de clientes

- El formulario administrativo cambia sus campos según el modo del router:
  - PPPoE solicita o genera usuario y contraseña.
  - DHCP solicita la **MAC WAN/CPE vista por MikroTik**.
- El formulario móvil del técnico también reconoce el modo del nodo y solicita
  la MAC cuando la instalación es DHCP.
- La MAC WAN/CPE se mantiene separada del identificador o serial de la ONU. Un
  serial GPON no se utiliza como MAC DHCP.
- Las MAC se normalizan al formato `AA:BB:CC:DD:EE:FF` y se rechazan valores
  inválidos.
- Para activar un servicio DHCP son obligatorias una IP válida y la MAC.
- Se evita que dos servicios vigentes del mismo router utilicen la misma MAC.
- También se evita asignar una IP que ya pertenezca a otra MAC o servicio.
- En los nodos DHCP no se generan credenciales PPPoE innecesarias.

## Aprovisionamiento MikroTik

Se incorporaron operaciones para:

- Listar leases DHCP de forma estricta y distinguir una lista vacía de un error
  de conexión.
- Consultar un lease por MAC.
- Crear un lease DHCP estático con IP, MAC, comentario y `rate-limit`.
- Actualizar un lease existente cuando cambie la IP o la velocidad.
- Convertir en estático un lease dinámico que ya corresponda a la MAC.
- Bloquear o desbloquear el acceso mediante `block-access`.
- Eliminar el lease cuando se elimina definitivamente al cliente.
- Confirmar después de cada escritura que MikroTik conservó el lease y la IP
  esperados.

El formato del límite DHCP incluye subida/bajada y, cuando el plan lo tiene
configurado, burst, umbral y tiempo. Los parámetros exclusivos del perfil PPP,
como prioridad y mínimo garantizado, no se envían al lease DHCP.

## Planes y control de velocidad

- Al activar o cambiar de plan se aplica inmediatamente el `rate-limit` del
  nuevo plan en el lease DHCP.
- Al editar un plan perteneciente a un nodo DHCP se actualizan los leases de
  sus servicios activos y suspendidos.
- Los servicios suspendidos conservan el bloqueo después de actualizar el
  plan.
- El botón **Auto-sync & reparar** del router sincroniza perfiles PPPoE o leases
  DHCP según el modo configurado.

## Suspensiones, pagos y bajas

- La suspensión de un servicio DHCP activa `block-access` y añade su IP a
  `CORTE_FDEZNET`.
- La reactivación desbloquea el lease y retira la IP de la lista de corte.
- La reactivación automática después de un pago o una promesa de pago también
  funciona para DHCP y vuelve a asegurar el lease con la velocidad vigente.
- La cancelación/baja bloquea el acceso DHCP.
- Si una baja se revierte, se recrea o actualiza el lease, se restaura el
  `rate-limit` y se desbloquea el acceso.
- La eliminación física del cliente elimina el lease DHCP correspondiente.
- Los flujos PPPoE existentes mantienen la desactivación del secret y el cierre
  de la sesión activa.

## Conciliación automática

El conciliador periódico de MikroTik, ejecutado cada cinco minutos, ahora
trabaja con ambos modos de acceso.

Para DHCP verifica y repara:

- Existencia del lease.
- Correspondencia de MAC e IP.
- `rate-limit` esperado según el plan.
- Estado de `block-access` según el estado activo o suspendido.
- Presencia o ausencia de la IP en `CORTE_FDEZNET`.

Las MAC duplicadas en un mismo router se registran como error y no se
sobrescriben automáticamente, evitando afectar al cliente equivocado. Todas las
reparaciones y fallos reintentables quedan en el historial de cronjobs.

## Portal y edición

- El portal técnico recibe el modo de seguridad del router, la MAC guardada y
  las credenciales sugeridas cuando corresponden.
- El detalle del cliente muestra credenciales PPPoE o MAC WAN/CPE según el
  nodo.
- La edición del cliente ya no confunde el identificador de la ONU con la MAC
  de acceso.
- La pantalla final del alta muestra la MAC en instalaciones DHCP y las
  credenciales en instalaciones PPPoE.

## Compatibilidad y base de datos

- No fue necesaria una nueva migración: los modelos y enumeraciones ya
  disponían de `mac_address`, `dhcp` y `colas_dinamicas`.
- Los routers existentes permanecen en PPPoE y no cambian automáticamente.
- La cabeza vigente de Alembic continúa siendo `f4a5b6c7d8e9`.

## Validación de calidad

- Backend: **263 pruebas aprobadas**.
- Pruebas específicas del adaptador DHCP: creación, conversión de lease
  dinámico, bloqueo, eliminación y prevención de IP duplicada.
- Conciliación DHCP: reparación de `rate-limit`, bloqueo y lista de corte.
- Frontend: ESLint sin errores ni advertencias.
- Compilación Vite/PWA de producción aprobada.
- Navegador: **90 pruebas E2E generales aprobadas**.
- Selector DHCP: **2 pruebas E2E adicionales**, escritorio y móvil.
- Auditoría npm: **0 vulnerabilidades encontradas**.
- Scripts de instalación/mantenimiento validados con `bash -n`.

## Liberación y producción

- Versión del sistema: `2.18.0`.
- Commit backend:
  `bcf6b423b9d65f177befb186643bcde5df84bf91`.
- Commit frontend:
  `fc2d41cf07f14988b76767c2b4bf6ea6ee9feb50`.
- Registro de liberación central: ID `35`.
- Respaldo previo verificado:
  `/var/backups/fdeznet/20260913-090139.tar.gz.gpg`.
- Producción quedó con `fdeznet-api`, `fdeznet-bot` y Nginx activos.
- Comprobaciones finales:
  - `/health/live`: correcta.
  - `/health/ready`: correcta.
  - `https://fdezpay.com/`: HTTP 200.
- La versión se asignó a las instalaciones con actualización automática
  desactivada. Cada cliente decide cuándo instalarla mediante el botón
  **Actualizar ahora**.

## Uso recomendado

1. Crear un nodo nuevo y seleccionar **DHCP estático (IP + MAC)**.
2. Confirmar que el servidor DHCP ya existe en RouterOS.
3. Crear la red IP y los planes asociados al nodo.
4. Durante la instalación, seleccionar la IP y capturar la MAC WAN/CPE que se
   observa en el lease DHCP de MikroTik.
5. No copiar el serial GPON de la ONU en el campo MAC.
6. Verificar que FastTrack no omita el tráfico sujeto a las colas dinámicas.
7. Utilizar **Auto-sync & reparar** si se requiere una conciliación manual
   inmediata.
