# Liberación de una versión para VPS

Una instalación nueva nunca debe tomar código arbitrario de `main`. El servidor
central entrega una versión activa con los SHA exactos de backend y frontend; el
instalador valida el formato y fija ambos repositorios en esos commits.

## Puerta de calidad

```bash
# Backend
python -m pytest -q
bash -n install.sh scripts/fdeznet-maintenance.sh
alembic heads

# Frontend
npm run audit:security
npm run check:release
```

Debe existir una sola cabeza de Alembic, no puede haber vulnerabilidades altas y
todas las pruebas deben aprobar. Después:

1. Subir backend y frontend a sus repositorios.
2. Desplegar el servidor central y ejecutar `alembic upgrade head`.
3. Publicar y dejar activa en **Licencias y versiones** la versión con ambos SHA
   completos antes de generar o canjear el token de instalación.
4. Probar la versión en una VPS piloto con la lista de aceptación de
   `docs/instalacion-vps.md`.
5. Asignarla primero a una instalación interna, después a un grupo pequeño y al
   final al resto de clientes.

Una licencia suspendida, revocada o vencida fuera del periodo de gracia bloquea
las rutas operativas y los webhooks, pero mantiene accesibles el inicio de sesión
y el estado de licencia para poder diagnosticar y renovar la instalación. Una
interrupción temporal del servidor central no detiene una licencia que fue
confirmada previamente como activa; la fecha de vencimiento guardada localmente
sí se aplica aunque el servidor central no responda.

Los planes, demos, límites y renovaciones se administran como se describe en
`docs/planes-licencia.md`.
