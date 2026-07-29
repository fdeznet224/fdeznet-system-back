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
        self.BASE_URL = os.getenv("WHATSAPP_BASE_URL") or (
            "http://whatsapp:3000"
            if os.environ.get("ENVIRONMENT") == "production"
            else "http://localhost:3000"
        )
        self.headers = {
            "X-Webhook-Secret": os.getenv("WEBHOOK_SECRET", ""),
        }
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
        resultado = await self.enviar_mensaje_detallado(telefono, mensaje, ruta)
        return resultado["ok"]

    async def enviar_mensaje_detallado(
        self,
        telefono: str,
        mensaje: str,
        ruta: str = None,
    ) -> dict:
        numero = self._formatear_numero(telefono)
        if not numero:
            return {"ok": False, "wa_id": None}

        payload = {
            "numero": numero,
            "mensaje": mensaje,
            "ruta": ruta,
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    self.TEXT_URL,
                    json=payload,
                    headers=self.headers,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    return {"ok": True, "wa_id": data.get("wa_id")}
                logger.error(
                    "Error del puente Node (%s): %s",
                    resp.status_code,
                    resp.text,
                )
                return {"ok": False, "wa_id": None}
        except Exception as exc:
            logger.error("Error al contactar con el puente Node: %s", exc)
            return {"ok": False, "wa_id": None}

class WhatsAppQueue:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.is_running = False
        self._processor_lock = asyncio.Lock()
        self.service = WhatsAppService()

    async def agregar_tarea(self, tarea: dict):
        await self.queue.put(tarea)
        async with self._processor_lock:
            if not self.is_running:
                self.is_running = True
                asyncio.create_task(self.procesar_cola())

    async def recuperar_pendientes(self, limite: int = 100):
        """Reencola salidas que quedaron sin ACK tras un reinicio."""
        from sqlalchemy import select
        from src.infrastructure.database import SessionLocal
        from src.infrastructure.models import MensajeChatModel

        async with SessionLocal() as db:
            registros = (
                await db.execute(
                    select(MensajeChatModel)
                    .where(
                        MensajeChatModel.direccion == "salida",
                        MensajeChatModel.ack == 0,
                    )
                    .order_by(MensajeChatModel.id.asc())
                    .limit(limite)
                )
            ).scalars().all()

        for registro in registros:
            await self.agregar_tarea({
                "numero": registro.telefono,
                "mensaje": registro.mensaje,
                "ruta": None,
                "intervalo": 0,
                "mensaje_chat_id": registro.id,
            })

    async def procesar_cola(self):
        try:
            while not self.queue.empty():
                tarea = await self.queue.get()

                try:
                    resultado = await self.service.enviar_mensaje_detallado(
                        telefono=tarea['numero'],
                        mensaje=tarea['mensaje'],
                        ruta=tarea.get('ruta'),
                    )

                    if resultado["ok"]:
                        tipo = "PDF + Texto" if tarea.get('ruta') else "Texto"
                        logger.info(
                            f"✅ Notificación ({tipo}) enviada a {tarea['numero']}"
                        )
                    else:
                        logger.error(
                            f"🚫 Falló envío a {tarea['numero']}. Revisa logs."
                        )
                    await self._actualizar_registro(
                        tarea.get("mensaje_chat_id"),
                        resultado,
                    )
                except Exception as e:
                    logger.error(f"❌ Fallo crítico en el hilo de la cola: {e}")
                    await self._actualizar_registro(
                        tarea.get("mensaje_chat_id"),
                        {"ok": False, "wa_id": None},
                    )

                # 🔥 MAGIA ANTI-BAN LEYENDO TU CONFIGURACIÓN EN MEMORIA 🔥
                intervalo_tarea = tarea.get('intervalo', 0)

                if intervalo_tarea > 0:
                    wait_time = intervalo_tarea
                else:
                    wait_time = GLOBAL_SETTINGS.get("intervalo_default", 60)

                logger.info(f"⏳ Escudo Anti-Ban: El motor descansará {wait_time} segundos...")
                await asyncio.sleep(wait_time)

                self.queue.task_done()
        finally:
            async with self._processor_lock:
                self.is_running = False
                if not self.queue.empty():
                    self.is_running = True
                    asyncio.create_task(self.procesar_cola())

    async def _actualizar_registro(self, mensaje_id, resultado):
        if not mensaje_id:
            return
        from src.infrastructure.database import SessionLocal
        from src.infrastructure.models import MensajeChatModel

        for intento in range(5):
            async with SessionLocal() as db:
                registro = await db.get(MensajeChatModel, mensaje_id)
                if registro:
                    registro.ack = 1 if resultado["ok"] else -1
                    registro.wa_id = resultado.get("wa_id")
                    await db.commit()
                    return
            if intento < 4:
                await asyncio.sleep(0.25)

# Instancia global para ser usada en todo el backend
whatsapp_queue = WhatsAppQueue()
