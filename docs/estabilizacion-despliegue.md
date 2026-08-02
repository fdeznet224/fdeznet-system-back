# Estabilización y despliegue

## Controles incorporados

- `GET /health/live` comprueba que el proceso HTTP responde.
- `GET /health/ready` ejecuta una consulta mínima contra la base de datos.
- Las rutas heredadas de bajas se identifican como obsoletas en OpenAPI.
- Las pruebas de contrato verifican rutas canónicas, compatibilidad y ausencia
  de operaciones HTTP duplicadas.
- El frontend carga cada pantalla bajo demanda y muestra una recuperación
  explícita cuando falla la descarga de un módulo.
- `npm run check:release` valida lint, construye la PWA y ejecuta los E2E.

Tras la división por rutas, el JavaScript inicial pasó de aproximadamente
1.7 MB a 389 KB (132 KB comprimido). Las pantallas de mayor peso se descargan
únicamente cuando se navega hacia ellas.

## Orden recomendado de publicación

1. Crear un respaldo verificable de la base de datos.
2. Publicar el backend y ejecutar `alembic upgrade head`.
3. Ejecutar `python -m pytest -q` (68 pruebas en la validación actual).
4. Comprobar `/api/health/live` y `/api/health/ready` a través del proxy.
5. En el frontend ejecutar `npm run check:release`.
6. Publicar el contenido generado en `dist`.
7. Validar con usuarios de cada rol los flujos de instalación, cobro,
   soporte, sincronización pendiente y baja con retiro.

Cuando se consulte el backend directamente, sin el proxy que monta `/api`, las
comprobaciones de salud se encuentran en `/health/live` y `/health/ready`.

## Regreso a una versión anterior

Los artefactos del frontend y la aplicación pueden regresar a la versión previa
si una comprobación funcional falla. Las migraciones recientes son aditivas;
después de recibir tráfico no se debe ejecutar un downgrade sin revisar primero
los datos generados en órdenes, finanzas, soporte, sincronización y bajas.

## Deuda técnica visible

- El lint completo y `lint:strict` pasan con cero errores y cero advertencias.
- La base de compatibilidad del navegador (`caniuse-lite`) necesita una
  actualización periódica.
- Quagga fue retirado porque no tenía consumidores; el escaneo vigente utiliza
  `@yudiel/react-qr-scanner`.
- Los horarios de generación de facturas y recordatorios forman parte del
  contrato de configuración y cuentan con una prueba de persistencia.
- Terminal de cobro, inventario, CRM y detalle técnico cuentan con contratos
  tipados y recorridos E2E en escritorio y móvil.
- Navegación global, facturas, ciclos de cobro y configuración de WhatsApp
  cuentan con contratos tipados y recorridos E2E en ambos perfiles.
- Órdenes, transacciones, estadísticas y mapa de red cuentan con contratos
  tipados y recorridos E2E en ambos perfiles.
- Usuarios, zonas, plantillas de mensajes, túneles VPN y cronjobs cuentan con
  contratos tipados y recorridos E2E en ambos perfiles.
- Cajas NAP, planes, redes IP, routers y diagnóstico de ping cuentan con
  contratos tipados y recorridos E2E en ambos perfiles. El puerto API
  predeterminado de MikroTik está alineado con el backend en `8728`.
- Inicio de sesión, actualización PWA, portal del cliente, búsqueda técnica y
  escáner QR cuentan con contratos tipados. La activación de una nueva versión
  del service worker ocurre antes de recargar la aplicación.
- El frontend completo se organiza por dominio; las pantallas, componentes,
  contratos, estilos y adaptadores exclusivos permanecen juntos. Los imports
  entre dominios usan el alias `@/`. La guía se encuentra en
  `frontend/docs/estructura.md`; la importación activa sigue en
  `pages/configuracion/Importar.tsx`.
- El adaptador OLT utiliza `/api` como respaldo en producción y conserva
  `localhost` únicamente durante el desarrollo local.
- El radar OLT reutiliza el adaptador tipado para monitoreo, consulta VSOL y
  normalización de respuestas; el alta y edición comparten el contrato del
  backend y cuentan con recorrido E2E.
- React Router conserva un aviso del modo RSC/acciones de servidor, no utilizado
  por esta SPA; debe actualizarse cuando exista una versión compatible corregida.
- Las cuatro rutas antiguas de bajas deben retirarse sólo después de medir que
  ya no existen consumidores.
