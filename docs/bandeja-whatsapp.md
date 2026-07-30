# Bandeja de salida de WhatsApp

## Estados

- `pendiente`: guardado en MySQL y esperando turno.
- `procesando`: un worker tomó el mensaje durante un máximo de dos minutos.
- `enviado`: WhatsApp aceptó el mensaje (`ack=1`).
- `entregado`: llegó al dispositivo (`ack=2`).
- `leido`: el destinatario lo abrió (`ack>=3`).
- `fallido`: hubo un error confirmado. Si es recuperable, se programa otro
  intento.
- `incierto`: el puente pudo haber enviado el mensaje, pero el backend perdió
  la respuesta. No se reintenta automáticamente para evitar duplicados.

Los reintentos automáticos usan pausas crecientes de 1, 5, 15, 60 y 180
minutos. El despachador revisa MySQL cada minuto y también recupera mensajes
que quedaron bloqueados por un reinicio.

## API operativa

- `GET /whatsapp/salidas`: lista paginada con filtros por estado, evento,
  cliente, texto, fecha y lote. Incluye totales por estado.
- `GET /whatsapp/salidas/{mensaje_id}`: detalle, intentos, error y tiempos.
- `POST /whatsapp/salidas/{mensaje_id}/reintentar`: reenvío manual de un
  mensaje fallido o incierto.
- `POST /whatsapp/salidas/reintentar-fallidos`: reenvío de hasta 500 mensajes,
  opcionalmente limitado a una lista de IDs.
- `GET /whatsapp/status`: salud del motor Node/Chromium.

Solo administración y supervisión pueden consultar o reintentar la bandeja.
Cada reintento guarda el usuario responsable.

## Persistencia

Las notificaciones automáticas, campañas, mensajes manuales, alertas de
routers y respuestas del bot se registran antes de enviarse. También se
conservan el adjunto, `wa_id`, lote, error, intentos y ACK.

El puente Node recibe `mensaje_chat_id` como clave idempotente. Si el backend
repite una solicitud dentro de la misma ejecución del motor, Node devuelve el
resultado anterior sin mandar nuevamente el mensaje.

## Operación

El proceso Node debe ejecutarse con reinicio automático, por ejemplo mediante
systemd o la política de reinicio del contenedor. Un reinicio ya no pierde la
cola: MySQL es la fuente de verdad y el backend vuelve a despacharla.

Un estado `incierto` requiere revisión humana del chat antes de pulsar
reenviar, porque el envío original pudo completarse durante un timeout.
