# Planes, demos y mensualidades

El servidor central administra los planes comerciales desde **Configuración →
Licencias y versiones**. Cada instalación conserva una copia de su plan, fecha
de vencimiento y límites; así puede aplicar las reglas incluso si pierde
temporalmente la conexión con el servidor central.

## Tipos de plan

- **Demo**: vence después de los días configurados y normalmente no tiene días
  de gracia.
- **Mensual**: renueva uno o más periodos desde la fecha de vencimiento actual.
  Si ya venció, la nueva vigencia comienza desde el momento de la renovación.
- **Permanente**: no tiene fecha de vencimiento. Puede conservar límites de
  abonados o routers, o dejarlos sin límite.

La migración crea cuatro plantillas iniciales: demo de 15 días, Básico,
Profesional e Ilimitado. Sus precios iniciales son cero para evitar asumir una
tarifa; el administrador debe crear o ajustar sus planes comerciales antes de
venderlos. Desactivar un plan impide asignarlo o renovarlo, pero no altera las
instalaciones que ya lo tienen.

## Alta y renovación

1. Cree el plan con código, nombre, precio, duración, gracia y límites.
2. Al crear una instalación, seleccione el plan. El dominio es opcional, por lo
   que una demo puede instalarse primero usando solamente la IP de la VPS.
3. El sistema inicia la vigencia al crear la instalación y muestra el consumo
   reportado de abonados y routers.
4. Al recibir el pago, use **Renovar 1 mes** y registre la referencia. La fecha se
   amplía y queda un movimiento en `pagos_licencia` con plan, meses, monto y hora.

El registro actual de pago es manual y auditable; todavía no cobra una tarjeta
ni confirma depósitos automáticamente. Una futura pasarela puede llamar la misma
operación de renovación después de validar su webhook.

## Vencimiento y bloqueo

Mientras el plan está vigente, la instalación opera normalmente. Después del
vencimiento pasa a **gracia** durante los días configurados y muestra un aviso
global. Al terminar la gracia cambia a **vencida**, muestra “Tu mensualidad
terminó. Renueva para continuar” y bloquea las rutas operativas.

El inicio de sesión y `GET /licencia/estado` siguen disponibles durante el
bloqueo para que el cliente vea el motivo. El heartbeat continúa consultando al
servidor central; por eso una renovación reactiva la instalación sin reinstalar
ni perder información.

## Límites

El alta individual y la importación masiva impiden superar el límite de abonados.
El alta de routers aplica el límite correspondiente. Los registros existentes no
se borran cuando se reduce un límite: el sistema bloquea nuevas altas hasta que
el uso vuelva a quedar dentro del plan o se asigne uno mayor.

Las instalaciones creadas antes de esta migración quedan sin vencimiento ni
límites hasta que el administrador les asigne expresamente un plan. Esto evita
bloquear clientes existentes durante la publicación.

## Prueba recomendada

1. Crear una demo corta en el servidor central y generar una instalación.
2. Instalarla en una VPS por IP y confirmar que reporta uso y vigencia.
3. Alcanzar los límites y comprobar que rechaza solamente nuevas altas.
4. Forzar una fecha vencida en el entorno piloto y comprobar aviso y bloqueo.
5. Renovar desde el servidor central y confirmar la reactivación en el siguiente
heartbeat (máximo 30 minutos), sin reinstalar la VPS. También puede usar
**Verificar ahora** para aplicar la renovación inmediatamente.
