import httpx
import logging
import os
import asyncio
import re

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class WhatsAppService:
    def __init__(self):
        self.BASE_URL = "http://whatsapp:3000" if os.environ.get("ENVIRONMENT") == "production" else "http://localhost:3000"
        self.TEXT_URL = f"{self.BASE_URL}/enviar-mensaje"

    def _formatear_numero(self, numero: str):
        """
        Limpia el número y fuerza el formato 521 para México.
        whatsapp-web.js lo necesita así para encontrar el contacto correctamente.
        """
        if not numero: return None
        
        # 1. Extraer solo dígitos
        num = "".join(re.findall(r'\d+', str(numero)))
        
        # 2. Lógica de normalización para México
        # Caso A: Viene solo el número (10 dígitos: 9613632496) -> Agregamos 521
        if len(num) == 10:
            return f"521{num}"
            
        # Caso B: Viene con 52 pero le falta el 1 (12 dígitos: 529613632496) -> Insertamos el 1
        if len(num) == 12 and num.startswith("52") and not num.startswith("521"):
            return f"521{num[2:]}"
            
        # Caso C: Ya viene con 521 (13 dígitos) -> Lo dejamos pasar tal cual
        return num

    async def enviar_mensaje(self, telefono: str, mensaje: str) -> bool:
        numero = self._formatear_numero(telefono)
        if not numero: return False
        
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                # Enviamos el número ya formateado como 521XXXXXXXXXX
                resp = await client.post(self.TEXT_URL, json={"numero": numero, "mensaje": mensaje})
                return resp.status_code == 200
        except Exception as e:
            logger.error(f"❌ Error al contactar con el puente de Node: {e}")
            return False

class WhatsAppQueue:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.is_running = False
        self.service = WhatsAppService()

    async def agregar_tarea(self, tarea: dict):
        await self.queue.put(tarea)
        if not self.is_running:
            asyncio.create_task(self.procesar_cola())

    async def procesar_cola(self):
        self.is_running = True
        while not self.queue.empty():
            tarea = await self.queue.get()
            wait_time = tarea.get('intervalo', 3) 
            
            try:
                exito = await self.service.enviar_mensaje(tarea['numero'], tarea['mensaje'])
                if exito:
                    logger.info(f"✅ Notificación enviada a {tarea['numero']}")
                else:
                    logger.error(f"🚫 Error 500 o 404 al enviar a {tarea['numero']}. Revisa el puente Node.")
            except Exception as e:
                logger.error(f"❌ Fallo crítico en cola: {e}")

            await asyncio.sleep(wait_time)
            self.queue.task_done()
        self.is_running = False

whatsapp_queue = WhatsAppQueue()