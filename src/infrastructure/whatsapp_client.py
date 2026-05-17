import httpx
import logging
import os
import asyncio
import re

# Configuración de Logs para ver qué pasa en la consola
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class WhatsAppService:
    def __init__(self):
        # Determina si usa el contenedor de Docker o Localhost
        self.BASE_URL = "http://whatsapp:3000" if os.environ.get("ENVIRONMENT") == "production" else "http://localhost:3000"
        self.TEXT_URL = f"{self.BASE_URL}/enviar-mensaje"

    def _formatear_numero(self, numero: str):
        """
        Limpia el número y fuerza el formato 521 para México.
        Necesario para que whatsapp-web.js encuentre el contacto.
        """
        if not numero: return None
        
        # 1. Extraer solo dígitos
        num = "".join(re.findall(r'\d+', str(numero)))
        
        # 2. Normalización para México
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
        """
        Agrega una tarea a la cola. 
        'tarea' debe ser un dict con: numero, mensaje y opcionalmente ruta.
        """
        await self.queue.put(tarea)
        if not self.is_running:
            asyncio.create_task(self.procesar_cola())

    async def procesar_cola(self):
        self.is_running = True
        while not self.queue.empty():
            tarea = await self.queue.get()
            
            # Tiempo de espera entre mensajes para evitar baneos de WhatsApp
            wait_time = tarea.get('intervalo', 5) 
            
            try:
                # 👇 PASAMOS LA RUTA AL SERVICIO DESDE LA TAREA 👇
                exito = await self.service.enviar_mensaje(
                    telefono=tarea['numero'], 
                    mensaje=tarea['mensaje'], 
                    ruta=tarea.get('ruta') # Si no existe, enviará None
                )
                
                if exito:
                    tipo = "PDF + Texto" if tarea.get('ruta') else "Texto"
                    logger.info(f"✅ Notificación ({tipo}) enviada a {tarea['numero']}")
                else:
                    logger.error(f"🚫 Falló envío a {tarea['numero']}. Revisa logs del puente Node.")
            except Exception as e:
                logger.error(f"❌ Fallo crítico en el hilo de la cola: {e}")

            # Esperar antes del siguiente mensaje
            await asyncio.sleep(wait_time)
            self.queue.task_done()
            
        self.is_running = False

# Instancia global para ser usada en todo el backend
whatsapp_queue = WhatsAppQueue()