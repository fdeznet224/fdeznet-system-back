# Bot de WhatsApp configurable

El administrador configura el autoservicio desde **Configuración > Flujo Bot WhatsApp**. El editor es un lienzo visual por nodos y conexiones, separado en **Bot clientes** y **Bot técnicos**.

Se puede cambiar sin editar código:

- activación general del bot;
- palabra o comando de acceso;
- duración de la sesión;
- inicio automático fuera del horario;
- mensaje de bienvenida y despedida;
- bloques de mensajes con texto libre;
- menús con una o varias rutas;
- acciones nativas del sistema;
- bloques de cierre y posición visual de cada nodo.

El flujo inicial incluye reporte de pago, promesa de pago, consulta de servicio y saldo, datos bancarios y diagnóstico técnico. Los datos bancarios se editan en **Configuración > Plantillas de Mensajes > Datos para Depósito o Transferencia** y nunca se guardan en el código fuente.

## Seguridad del autoservicio

Las consultas financieras, promesas y diagnósticos requieren que el contrato corresponda al número de teléfono registrado en el cliente. El reporte de pago conserva la posibilidad explícita de aplicar un pago a una cuenta familiar, sujeto a la conciliación bancaria existente.

El diagnóstico técnico es de solo lectura. Consulta, cuando las integraciones están disponibles:

- sesión PPPoE activa o desconectada;
- tiempo de conexión y respuesta de red;
- estado de la ONU;
- potencia óptica RX y TX.

El mensaje no expone credenciales PPPoE, contraseña del router, datos de acceso a la OLT ni información de otros clientes. Si MikroTik u OLT no responden dentro del tiempo de espera, el bot devuelve una respuesta segura y deriva la revisión a soporte.

## Bot privado para técnicos

El administrador registra el teléfono en **Configuración > Usuarios del sistema**, habilita **Bot técnico por WhatsApp** y asigna los routers permitidos. Los roles admitidos son administrador, supervisor y técnico.

El flujo técnico inicial se activa con `tecnico` y ofrece bloques independientes para:

- verificar sesión PPPoE, IP, uptime, ping y pérdida;
- verificar estado de ONU y potencia RX/TX;
- consultar router, usuario PPPoE, OLT, serial de ONU, caja NAP y puerto;
- ejecutar el diagnóstico completo.

Administradores y supervisores pueden consultar toda la red. Un técnico solo puede consultar clientes ligados a sus routers, clientes asignados directamente o contratos incluidos en una orden abierta a su nombre. Cada consulta queda en el registro de auditoría. El bot nunca envía contraseñas de clientes ni de infraestructura.
