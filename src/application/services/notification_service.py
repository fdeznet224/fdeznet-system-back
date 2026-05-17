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
            joinedload(ClienteModel.onu_asignada) # ✅ Cargamos la relación de la ONU
        ).where(ClienteModel.id == cliente_id)
        
        cliente = (await self.db.execute(stmt_c)).scalar_one_or_none()

        if not cliente or not cliente.telefono:
            logger.warning(f"⚠️ Cliente {cliente_id} no existe o no tiene teléfono.")
            return False

        # 3. EL DICCIONARIO MAESTRO
        datos_base = {
            "empresa": "FdezNet",
            "fecha_actual": datetime.now().strftime("%d/%m/%Y"),
            "nombre": cliente.nombre,
            "telefono": cliente.telefono,
            "direccion": cliente.direccion or "Domicilio conocido",
            "cedula": cliente.cedula or "Pendiente",
            
            # 🔥 CORRECCIÓN AQUÍ 🔥
            # En lugar de usar identificador_onu, entramos a la relación cargada
            "onu_serial": cliente.onu_asignada.identificador if cliente.onu_asignada else "N/A", 
            
            "ip": cliente.ip_asignada or "Pendiente",
            "nodo": cliente.router.nombre if cliente.router else "Principal",
            "plan": cliente.plan.nombre if cliente.plan else "Básico",
            "precio": f"${cliente.plan.precio}" if cliente.plan else "$0.00",
            "dia_corte": str(cliente.plantilla.dia_pago) if cliente.plantilla else "1",
            "usuario_pppoe": cliente.user_pppoe or "N/A",
            "pass_pppoe": cliente.pass_pppoe or "N/A",
        }

        # Unir con datos específicos del momento
        datos_finales = {**datos_base, **(variables_extra or {})}

        # 4. FORMATEAR MENSAJE
        mensaje_formateado = formatear_mensaje(plantilla.texto, datos_finales)
        
        # 5. ENCOLAR TAREA
        tarea = {
            "numero": cliente.telefono,
            "mensaje": mensaje_formateado,
            "ruta": ruta_pdf,
            "intervalo": 5 
        }
        
        await whatsapp_queue.agregar_tarea(tarea)
        logger.info(f"📨 Notificación '{tipo_evento}' encolada para {cliente.nombre}")
        return True