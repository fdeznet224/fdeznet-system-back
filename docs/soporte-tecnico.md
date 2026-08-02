# Soporte técnico y diagnóstico

El soporte utiliza las órdenes de servicio existentes. Cada incidencia conserva
cliente, categoría, canal de reporte, prioridad, técnico, estados, solución,
evidencias y tiempos de primera respuesta y resolución.

## Categorías

- `sin_internet`
- `lentitud`
- `potencia_baja`
- `router_wifi`
- `cable_roto`
- `cambio_domicilio`
- `otro`

No se permite abrir dos incidencias simultáneas de la misma categoría para el
mismo cliente.

## Flujo operativo

1. Crear la incidencia en `POST /soporte/incidencias`.
2. Consultarla en `GET /soporte/bandeja`.
3. Avanzar la orden a `trabajando`.
4. Ejecutar `POST /soporte/incidencias/{id}/diagnosticar`.
5. Registrar solución y cerrar mediante
   `POST /soporte/incidencias/{id}/resolver`.

Una reparación puede cerrarse con evidencia fotográfica o con al menos un
diagnóstico guardado. Las instalaciones conservan sus requisitos de evidencia
y conformidad.

## Diagnóstico consolidado

Cada ejecución guarda:

- disponibilidad del MikroTik;
- sesión PPPoE, IP, uptime y MAC reportada;
- ping y pérdida de paquetes;
- tráfico instantáneo de subida y bajada;
- estado de ONU y potencia RX/TX;
- integración OLT utilizada;
- errores parciales y recomendación automática.

El motor distingue ONU fuera de línea, potencia crítica, PPPoE sin sesión,
sesión sin ping, cliente conectado sin tráfico y probable problema local de
Wi-Fi. Si un equipo de gestión no responde, el resultado se marca como parcial
o incompleto en lugar de inventar un estado.

`GET /soporte/metricas` entrega volumen, terminadas y promedios de primera
respuesta y resolución por periodo.
