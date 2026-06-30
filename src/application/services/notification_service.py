from datetime import datetime
from sqlalchemy import select
from sqlalchemy.orm import joinedload
from sqlalchemy.ext.asyncio import AsyncSession
import logging

from src.infrastructure.models import PlantillaMensajeModel, ClienteModel
from src.application.helpers.message_formatter import formatear_mensaje
from src.infrastructure.whatsapp_client import whatsapp_queue

logger = logging.getLogger(__name__)

class NotificationService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def notificar(self, tipo_evento: str, cliente_id: int, variables_extra: dict = None, ruta_pdf: str = None):
        """
        MOTOR ÚNICO GLOBAL: Busca plantilla, extrae datos del cliente, 
        formatea y encola en WhatsApp (soporta texto y PDF).
        """
        
        # 1. BUSCAR PLANTILLA
        stmt_p = select(PlantillaMensajeModel).where(
            PlantillaMensajeModel.tipo == tipo_evento,
            PlantillaMensajeModel.activo == 1
        )
        plantilla = (await self.db.execute(stmt_p)).scalar_one_or_none()

        if not plantilla:
            logger.warning(f"⚠️ Plantilla '{tipo_evento}' no encontrada o inactiva.")
            return False

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

        # =========================================================
        # 3. CÁLCULOS INTELIGENTES (Las nuevas super variables)
        # =========================================================
        
        # A. Cálculos de Fechas
        dia_pago = cliente.plantilla.dia_pago if cliente.plantilla else 1
        dias_tolerancia = cliente.plantilla.dias_tolerancia if cliente.plantilla else 0
        
        # El día que se ejecuta el corte de servicio
        dia_corte_calc = dia_pago + dias_tolerancia
        dia_corte_servicio = dia_corte_calc if dia_corte_calc <= 30 else 30

        # El último día que el cliente tiene para pagar tranquilamente (un día antes del corte)
        ultimo_dia_pago = (dia_corte_servicio - 1) if dias_tolerancia > 0 else dia_corte_servicio

        # B. Mes en texto humano
        meses = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]
        mes_actual_nombre = meses[datetime.now().month - 1]

        # C. Conversión de velocidad (Kbps a Megas comerciales)
        velocidad = f"{int(cliente.plan.velocidad_bajada / 1024)} Megas" if cliente.plan and cliente.plan.velocidad_bajada else "Básico"

        # =========================================================
        # 4. EL DICCIONARIO MAESTRO BLINDADO
        # =========================================================
        datos_base = {
            "empresa": "FdezNet",
            "fecha_actual": datetime.now().strftime("%d/%m/%Y"),
            "mes_actual": mes_actual_nombre,
            "nombre": cliente.nombre,
            "telefono": cliente.telefono,
            "direccion": cliente.direccion or "Domicilio conocido",
            "cedula": cliente.cedula or "Pendiente",
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
            "monto_promesa": "$0.00"
        }

        # 5. UNIFICAR DATOS (Las variables de 'variables_extra' sobrescriben los valores por defecto)
        datos_finales = {**datos_base, **(variables_extra or {})}

        # 6. FORMATEAR MENSAJE
        mensaje_formateado = formatear_mensaje(plantilla.texto, datos_finales)
        
        # 7. ENCOLAR TAREA HACIA EL BOT DE WHATSAPP
        tarea = {
            "numero": cliente.telefono,
            "mensaje": mensaje_formateado,
            "ruta": ruta_pdf,
            "intervalo": 0 
        }
        
        await whatsapp_queue.agregar_tarea(tarea)
        logger.info(f"📨 Notificación '{tipo_evento}' encolada para {cliente.nombre}")
        
        return True