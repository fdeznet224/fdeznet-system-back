class NotificationEvent:
    FACTURA_EMITIDA = 'factura_emitida'
    RECORDATORIO_PAGO = 'recordatorio_pago'
    AVISO_CORTE = 'aviso_corte'
    CORTE_EJECUTADO = 'corte_ejecutado'
    PAGO_RECIBIDO = 'pago_recibido'
    ABONO_RECIBIDO = 'abono_recibido'
    RECONEXION = 'reconexion'
    PROMESA_PAGO = 'promesa_pago'
    PROMESA_VENCIDA = 'promesa_vencida'
    INSTALACION_ACTIVADA = 'instalacion_activada'
    CAMBIO_PLAN = 'cambio_plan'


LEGACY_EVENT_FALLBACKS = {
    NotificationEvent.FACTURA_EMITIDA: ('nueva_factura',),
    NotificationEvent.CORTE_EJECUTADO: ('aviso_corte',),
    NotificationEvent.AVISO_CORTE: ('aviso_corte',),
    NotificationEvent.RECONEXION: ('reconexion',),
    NotificationEvent.PAGO_RECIBIDO: ('pago_recibido',),
    NotificationEvent.ABONO_RECIBIDO: ('abono_recibido',),
}
