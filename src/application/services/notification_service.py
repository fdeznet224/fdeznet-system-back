from datetime import date, datetime, timedelta
from sqlalchemy import select
from sqlalchemy.orm import joinedload
from sqlalchemy.ext.asyncio import AsyncSession
import logging
import re

from src.infrastructure.models import (
    ClienteModel,
    ConfiguracionSistema,
    MensajeChatModel,
    PlantillaMensajeModel,
    ServicioModel,
)
from src.application.helpers.message_formatter import formatear_mensaje
from src.application.services.billing_calendar_service import BillingCalendarService
from src.infrastructure.whatsapp_client import whatsapp_queue

logger = logging.getLogger(__name__)

PLANTILLAS_OBLIGATORIAS = {
    "pago_recibido": (
        "✅ Hola {nombre}, recibimos tu pago por {monto_pagado}. "
        "Gracias por tu pago.\n\n{detalle_cobro}"
    ),
    "abono_recibido": (
        "✅ Hola {nombre}, recibimos tu abono por {monto_pagado}. "
        "{referencia}"
    ),
    "reconexion": (
        "✅ Hola {nombre}, tu servicio de internet fue reconectado."
    ),
    "recordatorio_promesa": (
        "⏰ Hola {nombre}, hoy vence tu promesa de pago. Tu saldo es "
        "{monto_promesa}. Paga hoy para que tu servicio siga activo; "
        "si ya pagaste, ignora este mensaje."
    ),
    "promesa_pago": (
        "✅ Hola {nombre}, registramos tu promesa de pago por "
        "{monto_promesa} con fecha límite {fecha_limite_promesa}. "
        "Si el saldo continúa pendiente, el servicio se suspenderá "
        "al día siguiente."
    ),
}


# Avisos donde el cliente debe ver el total real que debe (atrasos, extras y
# reconexión), no solo el monto de una factura.
EVENTOS_CON_TOTAL = {
    "nueva_factura",
    "recordatorio_pago",
    "corte_ejecutado",
    "aviso_corte",
    "corte_servicio",
}


def quitar_aviso_de_pdf(texto: str) -> str:
    """Plantillas antiguas prometen un recibo PDF adjunto que ya no se manda."""
    limpio = "\n".join(
        linea for linea in texto.splitlines()
        if not ("pdf" in linea.lower() and ("adjunt" in linea.lower() or "recibo" in linea.lower()))
    )
    return re.sub(r"\n{3,}", "\n\n", limpio)


class NotificationService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def notificar(
        self,
        tipo_evento: str,
        cliente_id: int,
        variables_extra: dict = None,
        ruta_pdf: str = None,
        clave_dedupe: str = None,
    ):
        """
        MOTOR ÚNICO GLOBAL: Busca plantilla, extrae datos del cliente, 
        formatea y encola en WhatsApp (soporta texto y PDF).
        """
        
        clave = (clave_dedupe or "").strip() or None
        if clave:
            existente = (
                await self.db.execute(
                    select(MensajeChatModel.id).where(
                        MensajeChatModel.clave_dedupe == clave
                    )
                )
            ).scalar_one_or_none()
            if existente:
                logger.info("Notificación duplicada omitida: %s", clave)
                return False

        # 1. BUSCAR PLANTILLA
        stmt_p = select(PlantillaMensajeModel).where(
            PlantillaMensajeModel.tipo == tipo_evento,
            PlantillaMensajeModel.activo == 1
        )
        plantilla = (await self.db.execute(stmt_p)).scalar_one_or_none()

        texto_plantilla = (
            plantilla.texto
            if plantilla
            else PLANTILLAS_OBLIGATORIAS.get(tipo_evento)
        )

        if not texto_plantilla:
            logger.warning(f"⚠️ Plantilla '{tipo_evento}' no encontrada o inactiva.")
            return False
        if not plantilla:
            logger.warning(
                "Plantilla '%s' ausente o inactiva; se usó el mensaje "
                "obligatorio del sistema.",
                tipo_evento,
            )

        # 2. BUSCAR CLIENTE CON TODA SU INFO (Cargamos todas las relaciones necesarias)
        stmt_c = select(ClienteModel).options(
            joinedload(ClienteModel.plan),
            joinedload(ClienteModel.plantilla),
            joinedload(ClienteModel.router),
            joinedload(ClienteModel.onu_asignada),
            joinedload(ClienteModel.zona)
        ).where(ClienteModel.id == cliente_id)
        
        cliente = (await self.db.execute(stmt_c)).scalar_one_or_none()

        if not cliente or not cliente.telefono:
            logger.warning(f"⚠️ Cliente {cliente_id} no existe o no tiene teléfono.")
            return False

        marca = (
            await self.db.execute(
                select(ConfiguracionSistema).where(ConfiguracionSistema.id == 1)
            )
        ).scalar_one_or_none()
        empresa_nombre = getattr(marca, "empresa_nombre", None) or "FdezNet"

        # =========================================================
        # 3. CÁLCULOS INTELIGENTES (Las nuevas super variables)
        # =========================================================
        
        # A. Cálculos de Fechas
        # El día de pago sale del servicio: el fijo de su plantilla o el de
        # su instalación, según la forma de cobro.
        servicio = (
            await self.db.execute(
                select(ServicioModel)
                .options(joinedload(ServicioModel.plantilla))
                .where(
                    ServicioModel.cliente_id == cliente.id,
                    ServicioModel.estado.in_(["activo", "suspendido"]),
                )
                .order_by(ServicioModel.id.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        plantilla_cobro = (
            (servicio.plantilla if servicio else None) or cliente.plantilla
        )
        if servicio:
            dia_pago = BillingCalendarService.dia_ciclo_servicio(
                servicio, plantilla_cobro
            )
        else:
            dia_pago = plantilla_cobro.dia_pago if plantilla_cobro else 1
        dias_tolerancia = (
            (plantilla_cobro.dias_tolerancia or 0) if plantilla_cobro else 0
        )

        # El corte cae N días después del próximo pago; puede pasar al mes
        # siguiente (pago el 30 + 3 días = corte el 3).
        hoy = date.today()
        proximo_pago = BillingCalendarService.siguiente_inicio_ciclo(
            hoy - timedelta(days=1), dia_pago
        )
        fecha_corte = proximo_pago + timedelta(days=dias_tolerancia)
        dia_corte_servicio = fecha_corte.day

        # El último día que el cliente tiene para pagar tranquilamente (un día antes del corte)
        ultimo_dia_pago = (
            (fecha_corte - timedelta(days=1)).day
            if dias_tolerancia > 0
            else dia_corte_servicio
        )

        # B. Mes en texto humano
        meses = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]
        mes_actual_nombre = meses[datetime.now().month - 1]

        # C. Conversión de velocidad (Kbps a Megas comerciales)
        velocidad = f"{int(cliente.plan.velocidad_bajada / 1024)} Megas" if cliente.plan and cliente.plan.velocidad_bajada else "Básico"

        # =========================================================
        # 4. EL DICCIONARIO MAESTRO BLINDADO
        # =========================================================
        datos_base = {
            "empresa": empresa_nombre,
            "fecha_actual": datetime.now().strftime("%d/%m/%Y"),
            "mes_actual": mes_actual_nombre,
            "nombre": cliente.nombre,
            "telefono": cliente.telefono,
            "direccion": cliente.direccion or "Domicilio conocido",
            "cedula": cliente.cedula or "Pendiente",
            "contrato": cliente.cedula or "Pendiente",
            "zona": cliente.zona.nombre if cliente.zona else "Cobertura General",
            
            # Hardware e IP
            "onu_serial": cliente.onu_asignada.identificador if cliente.onu_asignada else "N/A", 
            "ip": cliente.ip_asignada or "Pendiente",
            "nodo": cliente.router.nombre if cliente.router else "Principal",
            "usuario_pppoe": cliente.user_pppoe or "N/A",
            "pass_pppoe": cliente.pass_pppoe or "N/A",
            
            # Servicio y Finanzas Básicas
            "plan": cliente.plan.nombre if cliente.plan else "Básico",
            "precio": f"${cliente.plan.precio}" if cliente.plan else "$0.00",
            "velocidad": velocidad,
            
            # 🔥 NUEVAS VARIABLES DE FECHAS CLARAS 🔥
            "dia_inicio_pago": str(dia_pago),             # Ej: 1
            "ultimo_dia_pago": str(ultimo_dia_pago),      # Ej: 5
            "dia_corte_servicio": str(dia_corte_servicio),# Ej: 6
            
            # (Se mantienen las viejas para no romper plantillas anteriores)
            "dia_corte": str(dia_pago),
            "dia_final": str(dia_corte_servicio),
            
            "saldo_favor": f"${cliente.saldo_a_favor}" if cliente.saldo_a_favor else "$0.00",
            "estado_cliente": cliente.estado.capitalize(),

            # Valores por defecto...
            "monto_pagado": "$0.00",
            "referencia": "N/A",
            "fecha_limite_promesa": "N/A",
            "monto_promesa": "$0.00",
            "detalle_cobro": "",
            "periodo_desde": "N/A",
            "periodo_hasta": "N/A",
            "dias_con_servicio": "0",
            "dias_sin_servicio": "0",
            "monto_servicio_original": "$0.00",
            "ajuste_suspension": "$0.00",
            "cargos_adicionales": "$0.00",
            "total_factura": "$0.00",
            "total_a_pagar": "$0.00",
            "desglose_total": "",
        }

        # 5. UNIFICAR DATOS (Las variables de 'variables_extra' sobrescriben los valores por defecto)
        datos_finales = {**datos_base, **(variables_extra or {})}

        # 6. FORMATEAR MENSAJE
        mensaje_formateado = formatear_mensaje(
            texto_plantilla,
            datos_finales,
        )
        detalle_cobro = str(datos_finales.get("detalle_cobro") or "").strip()
        if (
            tipo_evento in {"nueva_factura", "pago_recibido"}
            and detalle_cobro
            and "{detalle_cobro}" not in texto_plantilla
            and detalle_cobro not in mensaje_formateado
        ):
            mensaje_formateado = f"{mensaje_formateado}\n\n{detalle_cobro}"
        # El recibo va escrito en el mensaje (ya no se adjunta PDF).
        recibo = str(datos_finales.get("recibo") or "").strip()
        if tipo_evento == "pago_recibido" and recibo and "{recibo}" not in texto_plantilla:
            mensaje_formateado = f"{mensaje_formateado}\n\n{recibo}"
        if not ruta_pdf:
            mensaje_formateado = quitar_aviso_de_pdf(mensaje_formateado)
        # Si hay atrasos o extras, el cliente ve el total real a pagar aunque
        # su plantilla no use {total_a_pagar} ni {desglose_total}.
        desglose_total = str(datos_finales.get("desglose_total") or "").strip()
        if (
            tipo_evento in EVENTOS_CON_TOTAL
            and desglose_total
            and "{desglose_total}" not in texto_plantilla
            and "{total_a_pagar}" not in texto_plantilla
        ):
            mensaje_formateado = f"{mensaje_formateado}\n\n{desglose_total}"
        
        # 7. ENCOLAR TAREA HACIA EL BOT DE WHATSAPP
        registro = MensajeChatModel(
            cliente_id=cliente.id,
            telefono=whatsapp_queue.service._formatear_numero(cliente.telefono),
            direccion="salida",
            mensaje=mensaje_formateado,
            tipo_mensaje="documento" if ruta_pdf else "texto",
            tipo_evento=tipo_evento,
            clave_dedupe=clave,
            leido=True,
            ack=0,
            estado_envio="pendiente",
            ruta_archivo=ruta_pdf,
        )
        self.db.add(registro)
        await self.db.flush()
        # La salida queda persistida antes de entregarla al proceso asíncrono;
        # así la clave anti-duplicado y los ACK sobreviven reinicios.
        await self.db.commit()

        tarea = {
            # Se incluyen también los datos por compatibilidad con consumidores
            # internos; el worker durable vuelve a leerlos desde MySQL.
            "numero": cliente.telefono,
            "mensaje": mensaje_formateado,
            "ruta": ruta_pdf,
            "intervalo": 0,
            "mensaje_chat_id": registro.id,
        }
        
        await whatsapp_queue.agregar_tarea(tarea)
        logger.info(f"📨 Notificación '{tipo_evento}' encolada para {cliente.nombre}")
        
        return True
