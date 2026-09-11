# Bot de WhatsApp configurable

El administrador configura el autoservicio desde **Configuración > Flujo Bot WhatsApp**.

Se puede cambiar sin editar código:

- activación general del bot;
- palabra o comando de acceso;
- duración de la sesión;
- inicio automático fuera del horario;
- mensaje de bienvenida y despedida;
- nombre, estado y orden de las opciones del menú.

El flujo inicial incluye reporte de pago, promesa de pago, consulta de servicio y saldo, datos bancarios y diagnóstico técnico. Los datos bancarios se editan en **Configuración > Plantillas de Mensajes > Datos para Depósito o Transferencia** y nunca se guardan en el código fuente.

## Seguridad del autoservicio

Las consultas financieras, promesas y diagnósticos requieren que el contrato corresponda al número de teléfono registrado en el cliente. El reporte de pago conserva la posibilidad explícita de aplicar un pago a una cuenta familiar, sujeto a la conciliación bancaria existente.

El diagnóstico técnico es de solo lectura. Consulta, cuando las integraciones están disponibles:

- sesión PPPoE activa o desconectada;
- tiempo de conexión y respuesta de red;
- estado de la ONU;
- potencia óptica RX y TX.

El mensaje no expone credenciales PPPoE, contraseña del router, datos de acceso a la OLT ni información de otros clientes. Si MikroTik u OLT no responden dentro del tiempo de espera, el bot devuelve una respuesta segura y deriva la revisión a soporte.
