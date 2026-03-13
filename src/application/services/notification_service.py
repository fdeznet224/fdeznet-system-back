from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import joinedload
import logging

logger = logging.getLogger(__name__)

from src.infrastructure.models import PlantillaMensajeModel, ClienteModel

# 👇 IMPORTANTE: Importamos la COLA GLOBAL, no solo el servicio
from src.infrastructure.whatsapp_client import whatsapp_queue

class NotificationService:
    def __init__(self, db: AsyncSession):
        self.db = db
        # Ya no instanciamos WhatsAppService aquí, usamos la cola global importada

    async def notificar_evento(self, evento: str, cliente_id: int, variables: dict = None, intervalo: int = 60):
        """
        Busca plantilla, reemplaza variables y ENCOLA el mensaje.
        :param intervalo: Tiempo en segundos a esperar después de enviar este mensaje (Default 60s)
        """
        
        # 1. BUSCAR PLANTILLA
        stmt_plantilla = select(PlantillaMensajeModel).where(
            PlantillaMensajeModel.tipo == evento,
            PlantillaMensajeModel.activo == True
        )
        plantilla = (await self.db.execute(stmt_plantilla)).scalar_one_or_none()

        if not plantilla:
            return False

        # 2. OBTENER CLIENTE
        stmt_cliente = select(ClienteModel).options(
            joinedload(ClienteModel.plan),
            joinedload(ClienteModel.plantilla)
        ).where(ClienteModel.id == cliente_id)
        
        cliente = (await self.db.execute(stmt_cliente)).scalar_one_or_none()

        if not cliente or not cliente.telefono:
            logger.warning(f"⚠️ Cliente {cliente_id} sin teléfono para evento {evento}")
            return False

        # 3. PREPARAR DATOS
        nombre_plan = cliente.plan.nombre if cliente.plan else "N/A"
        precio_plan = f"${cliente.plan.precio}" if cliente.plan else "$0.00"
        dia_corte = str(cliente.plantilla.dia_pago) if cliente.plantilla else "1"

        datos_base = {
            "nombre": cliente.nombre,
            "telefono": cliente.telefono,
            "direccion": cliente.direccion or "",
            "ip": cliente.ip_asignada or "",
            "plan": nombre_plan,
            "precio": precio_plan,
            "dia_corte": dia_corte,
            "fecha_actual": datetime.now().strftime("%d/%m/%Y"),
            "empresa": "FdezNet",
        }
        
        datos_finales = {**datos_base, **(variables or {})}

        # 4. REEMPLAZO DE VARIABLES
        mensaje_final = plantilla.texto
        for clave, valor in datos_finales.items():
            marcador = f"{{{clave}}}" 
            if marcador in mensaje_final:
                mensaje_final = mensaje_final.replace(marcador, str(valor))

        # 5. 👇 AQUÍ EL CAMBIO: AGREGAR A LA COLA
        logger.info(f"📨 Encolando '{evento}' para {cliente.nombre} (Espera: {intervalo}s)")
        
        tarea = {
            "numero": cliente.telefono,
            "mensaje": mensaje_final,
            "ruta": None,
            "intervalo": intervalo # 👈 Pasamos el tiempo que decidió el Frontend o Default
        }
        
        await whatsapp_queue.agregar_tarea(tarea)
        
        return True

    async def enviar_factura_pdf(self, cliente_id: int, ruta_pdf: str, mensaje_opcional: str = None, intervalo: int = 60):
        """
        Encola el envío de un PDF.
        """
        cliente = await self.db.get(ClienteModel, cliente_id)
        if not cliente or not cliente.telefono: return False
        
        texto = mensaje_opcional or f"Hola {cliente.nombre}, adjuntamos tu comprobante."
        
        logger.info(f"📎 Encolando PDF para {cliente.nombre}")
        
        tarea = {
            "numero": cliente.telefono,
            "mensaje": texto,
            "ruta": ruta_pdf,
            "intervalo": intervalo # 👈 Tiempo seguro
        }
        
        await whatsapp_queue.agregar_tarea(tarea)
        return True