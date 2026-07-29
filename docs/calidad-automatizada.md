# Calidad automatizada

## Backend

Los esquemas ORM usan `ConfigDict(from_attributes=True)` de Pydantic 2. La
suite valida además que las listas predeterminadas no compartan estado entre
instancias.

Comandos:

```bash
python -m pytest -q
python -m compileall -q src
```

La validación actual contiene 68 pruebas. Los avisos de configuración de
Pydantic 1 fueron eliminados; permanecen avisos externos de Passlib y EasyOCR.

## Frontend

El lint global ya opera en modo estricto:

- `npm run lint`: cero errores y cero advertencias.
- `npm run lint:critical`: cero errores y cero advertencias en PWA, bajas y
  arranque de la aplicación.
- `npm run lint:strict`: comprobación estricta explícita de todo el frontend.

Tanto `lint` como `lint:strict` usan `--max-warnings 0`; cualquier advertencia
nueva hace fallar la validación.

## Pruebas E2E

Playwright ejecuta los escenarios en Chrome de escritorio y emulación móvil:

1. Renderizado del acceso.
2. Aviso al perder conectividad.
3. Carga diferida de la pantalla de bajas con API simulada.
4. Recuperación visible cuando falla la descarga de un módulo.
5. Carga del panel técnico con sesión de técnico y API simulada.
6. Carga del radar OLT activo con respuesta vacía en ambos diseños.
7. Carga del panel principal con contratos de métricas y facturación simulados.
8. Apertura de herramientas y alta desde el listado unificado de clientes.
9. Persistencia visual de los horarios configurables de cronjobs.
10. Carga de la importación masiva con catálogos vacíos.
11. Carga del panel de cobranza con caja cerrada.
12. Carga de una instalación técnica con infraestructura preasignada.
13. Apertura de la terminal de cobro y búsqueda predictiva de un cliente.
14. Carga del inventario con un equipo disponible.
15. Apertura de una conversación desde el CRM.
16. Carga del detalle técnico completo de un cliente.
17. Búsqueda de un cliente desde el encabezado global.
18. Carga de facturas y su resumen financiero.
19. Carga de la configuración activa del motor WhatsApp.
20. Apertura del formulario para crear un ciclo de cobro.
21. Carga del módulo administrativo de órdenes.
22. Carga de transacciones y sus filtros financieros.
23. Carga de estadísticas de ingresos.
24. Carga del mapa desde el módulo de monitoreo.
25. Carga de usuarios y routers asignados.
26. Carga de la administración de zonas.
27. Carga de plantillas de mensajes de WhatsApp.
28. Carga de la infraestructura de túneles VPN.
29. Carga del historial de cronjobs.
30. Carga del inventario de cajas NAP.
31. Carga de la administración de planes de internet.
32. Carga de la administración de redes IP.
33. Carga y visualización de nodos MikroTik.
34. Inicio de sesión con redirección al panel administrativo.
35. Carga del portal público del cliente.
36. Búsqueda de abonados desde la herramienta técnica.
37. Apertura de la herramienta móvil de escaneo QR.
38. Alta de una OLT mediante el formulario administrativo tipado.

Comandos:

```bash
npm run test:e2e
npm run test:e2e:headed
npm run check:release
```

`check:release` ejecuta lint completo, lint crítico, compilación PWA y los
76 casos resultantes de los 38 escenarios en ambos perfiles.

## Dependencias

`npm audit fix` se utiliza sin `--force`. Axios y dependencias transitivas se
actualizaron, React Router quedó fijado en `7.18.2` y Quagga fue eliminado
porque no tenía importaciones. El escáner activo utiliza
`@yudiel/react-qr-scanner`.

La auditoría de producción conserva dos hallazgos altos del mismo riesgo:

- Dos nodos corresponden a React Router y React Router DOM por un aviso del modo
  RSC/acciones de servidor que esta SPA no utiliza. Debe revisarse cuando exista
  una versión compatible corregida.
