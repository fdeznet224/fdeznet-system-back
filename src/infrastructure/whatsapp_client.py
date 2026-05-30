#src/infraestutura/whsapp_client.py

import httpx
import logging
import os
import asyncio
import re

# Configuración de Logs para ver qué pasa en la consola
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

GLOBAL_SETTINGS = {"intervalo_default": 60}

class WhatsAppService:
    def __init__(self):
        # Determina si usa el contenedor de Docker o Localhost
        self.BASE_URL = "http://whatsapp:3000" if os.environ.get("ENVIRONMENT") == "production" else "http://localhost:3000"
        self.TEXT_URL = f"{self.BASE_URL}/enviar-mensaje"

    def _formatear_numero(self, numero: str):
        if not numero: return None
        
        # 🔥 CORRECCIÓN: Si es un LID o ya trae arroba, no lo formatees, pásalo intacto
        if "@" in str(numero):
            return str(numero)
            
        num = "".join(re.findall(r'\d+', str(numero)))
        
        if len(num) == 10:
            return f"521{num}"
            
        if len(num) == 12 and num.startswith("52") and not num.startswith("521"):
            return f"521{num[2:]}"
            
        return num

    async def enviar_mensaje(self, telefono: str, mensaje: str, ruta: str = None) -> bool:
        """
        Envía la petición POST al puente de Node.js.
        Soporta un campo opcional 'ruta' para enviar archivos PDF.
        """
        numero = self._formatear_numero(telefono)
        if not numero: return False
        
        # --- 🚀 PAYLOAD UNIFICADO ---
        # Enviamos 'ruta' (será None para texto simple y un String para cobros con PDF)
        payload = {
            "numero": numero, 
            "mensaje": mensaje,
            "ruta": ruta 
        }
        
        try:
            # Aumentamos el timeout a 30s porque enviar archivos pesados toma más tiempo
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(self.TEXT_URL, json=payload)
                
                if resp.status_code == 200:
                    return True
                else:
                    logger.error(f"🚫 Error del puente Node ({resp.status_code}): {resp.text}")
                    return False
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
            
            try:
                exito = await self.service.enviar_mensaje(
                    telefono=tarea['numero'], 
                    mensaje=tarea['mensaje'], 
                    ruta=tarea.get('ruta') 
                )
                
                if exito:
                    tipo = "PDF + Texto" if tarea.get('ruta') else "Texto"
                    logger.info(f"✅ Notificación ({tipo}) enviada a {tarea['numero']}")
                else:
                    logger.error(f"🚫 Falló envío a {tarea['numero']}. Revisa logs.")
            except Exception as e:
                logger.error(f"❌ Fallo crítico en el hilo de la cola: {e}")

            # 🔥 MAGIA ANTI-BAN LEYENDO TU CONFIGURACIÓN EN MEMORIA 🔥
            # 1. Checa si la tarea trae un intervalo específico (como en tu enviar_campana)
            # 2. Si no trae (como las notificaciones automáticas), lee el GLOBAL_SETTINGS
            intervalo_tarea = tarea.get('intervalo', 0)
            
            if intervalo_tarea > 0:
                wait_time = intervalo_tarea
            else:
                wait_time = GLOBAL_SETTINGS.get("intervalo_default", 60)

            logger.info(f"⏳ Escudo Anti-Ban: El motor descansará {wait_time} segundos...")
            await asyncio.sleep(wait_time)
            
            self.queue.task_done()
            
        self.is_running = False

# Instancia global para ser usada en todo el backend
whatsapp_queue = WhatsAppQueue()