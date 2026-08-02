#src/infraestutura/whsapp_client.py

import httpx
import logging
import os
import asyncio
import re
from datetime import datetime, timedelta

# Configuración de Logs para ver qué pasa en la consola
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

GLOBAL_SETTINGS = {"intervalo_default": 60}
ESTADOS_FINALES = {"entregado", "leido"}
RETRASOS_REINTENTO_SEGUNDOS = (60, 300, 900, 3600, 10800)


def set_intervalo_default(segundos: int) -> int:
    segundos = max(1, min(int(segundos), 3600))
    GLOBAL_SETTINGS["intervalo_default"] = segundos
    return segundos


def estado_por_ack(ack: int) -> str:
    if ack < 0:
        return "fallido"
    if ack == 0:
        return "pendiente"
    if ack == 1:
        return "enviado"
    if ack == 2:
        return "entregado"
    return "leido"

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

    async def enviar_mensaje(
        self,
        telefono: str,
        mensaje: str,
        ruta: str = None,
        tipo_evento: str = "bot_respuesta",
    ) -> bool:
        """Envía una respuesta inmediata sin sacarla de la outbox durable."""
        from sqlalchemy import String, cast, select
        from src.infrastructure.database import SessionLocal
        from src.infrastructure.models import ClienteModel, MensajeChatModel

        numero = self._formatear_numero(telefono)
        cliente_id = None
        async with SessionLocal() as db:
            ultimos_10 = (numero or "").split("@")[0][-10:]
            if ultimos_10:
                cliente_id = (
                    await db.execute(
                        select(ClienteModel.id)
                        .where(
                            cast(ClienteModel.telefono, String).like(
                                f"%{ultimos_10}%"
                            )
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
            registro = MensajeChatModel(
                cliente_id=cliente_id,
                telefono=numero,
                direccion="salida",
                mensaje=mensaje,
                tipo_mensaje="documento" if ruta else "texto",
                tipo_evento=tipo_evento,
                leido=True,
                ack=0,
                estado_envio="procesando",
                intentos=1,
                ultima_tentativa_en=datetime.now(),
                bloqueado_hasta=datetime.now() + timedelta(minutes=2),
                ruta_archivo=ruta,
            )
            db.add(registro)
            await db.commit()

        resultado = await self.enviar_mensaje_detallado(
            telefono,
            mensaje,
            ruta,
            mensaje_chat_id=registro.id,
        )
        await whatsapp_queue._actualizar_registro(registro.id, resultado)
        return resultado["ok"]

    async def enviar_mensaje_detallado(
        self,
        telefono: str,
        mensaje: str,
        ruta: str = None,
        mensaje_chat_id: int | None = None,
    ) -> dict:
        numero = self._formatear_numero(telefono)
        if not numero:
            return {
                "ok": False,
                "wa_id": None,
                "error": "El destinatario no tiene un número válido",
                "reintentable": False,
                "incierto": False,
            }

        payload = {
            "numero": numero,
            "mensaje": mensaje,
            "ruta": ruta,
            "mensaje_chat_id": mensaje_chat_id,
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
                    return {
                        "ok": True,
                        "wa_id": data.get("wa_id"),
                        "error": None,
                        "reintentable": False,
                        "incierto": False,
                    }
                try:
                    detalle = resp.json().get("error")
                except Exception:
                    detalle = resp.text
                logger.error(
                    "Error del puente Node (%s): %s",
                    resp.status_code,
                    resp.text,
                )
                # Un 500 del puente puede ocurrir después de que WhatsApp
                # aceptó el envío. Reintentarlo automáticamente puede
                # duplicar el mensaje; requiere revisión/reenvío manual.
                if resp.status_code == 500:
                    return {
                        "ok": False,
                        "wa_id": None,
                        "error": (
                            f"Puente WhatsApp HTTP {resp.status_code}: "
                            f"{detalle or 'sin detalle'}"
                        )[:2000],
                        "reintentable": False,
                        "incierto": True,
                    }
                return {
                    "ok": False,
                    "wa_id": None,
                    "error": (
                        f"Puente WhatsApp HTTP {resp.status_code}: "
                        f"{detalle or 'sin detalle'}"
                    )[:2000],
                    "reintentable": resp.status_code in {
                        429,
                        500,
                        502,
                        503,
                        504,
                    },
                    "incierto": False,
                }
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            logger.error("No se pudo conectar con el puente Node: %s", exc)
            return {
                "ok": False,
                "wa_id": None,
                "error": f"Puente WhatsApp no disponible: {exc}"[:2000],
                "reintentable": True,
                "incierto": False,
            }
        except (
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.PoolTimeout,
        ) as exc:
            logger.error("Resultado incierto enviando a WhatsApp: %s", exc)
            return {
                "ok": False,
                "wa_id": None,
                "error": (
                    "Tiempo de espera agotado después de iniciar el envío; "
                    "verifica el teléfono antes de reenviar"
                ),
                "reintentable": False,
                "incierto": True,
            }
        except Exception as exc:
            logger.error("Error al contactar con el puente Node: %s", exc)
            return {
                "ok": False,
                "wa_id": None,
                "error": str(exc)[:2000],
                "reintentable": False,
                "incierto": False,
            }

class WhatsAppQueue:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.is_running = False
        self._processor_lock = asyncio.Lock()
        self._queued_ids: set[int] = set()
        self.service = WhatsAppService()

    async def agregar_tarea(self, tarea: dict):
        mensaje_id = tarea.get("mensaje_chat_id")
        if mensaje_id:
            async with self._processor_lock:
                if mensaje_id in self._queued_ids:
                    return False
                self._queued_ids.add(mensaje_id)
        await self.queue.put(tarea)
        async with self._processor_lock:
            if not self.is_running:
                self.is_running = True
                asyncio.create_task(self.procesar_cola())
        return True

    async def recuperar_pendientes(self, limite: int = 500):
        """Despacha pendientes, fallos vencidos y bloqueos abandonados."""
        from sqlalchemy import and_, or_, select
        from src.infrastructure.database import SessionLocal
        from src.infrastructure.models import (
            ConfiguracionModel,
            MensajeChatModel,
        )

        ahora = datetime.now()
        async with SessionLocal() as db:
            intervalo_guardado = (
                await db.execute(
                    select(ConfiguracionModel.valor).where(
                        ConfiguracionModel.clave
                        == "whatsapp_intervalo_segundos"
                    )
                )
            ).scalar_one_or_none()
            if intervalo_guardado:
                try:
                    set_intervalo_default(int(intervalo_guardado))
                except (TypeError, ValueError):
                    logger.warning(
                        "Intervalo WhatsApp guardado inválido: %s",
                        intervalo_guardado,
                    )
            registros = (
                await db.execute(
                    select(MensajeChatModel.id)
                    .where(
                        MensajeChatModel.direccion == "salida",
                        MensajeChatModel.estado_envio != "eliminado",
                        MensajeChatModel.intentos
                        < MensajeChatModel.max_intentos,
                        or_(
                            MensajeChatModel.estado_envio == "pendiente",
                            and_(
                                MensajeChatModel.estado_envio == "fallido",
                                MensajeChatModel.proximo_intento_en.isnot(
                                    None
                                ),
                                MensajeChatModel.proximo_intento_en <= ahora,
                            ),
                            and_(
                                MensajeChatModel.estado_envio
                                == "procesando",
                                MensajeChatModel.bloqueado_hasta.isnot(None),
                                MensajeChatModel.bloqueado_hasta <= ahora,
                            ),
                        ),
                    )
                    .order_by(MensajeChatModel.id.asc())
                    .limit(limite)
                )
            ).scalars().all()

        encolados = 0
        for mensaje_id in registros:
            agregado = await self.agregar_tarea({
                "intervalo": 0,
                "mensaje_chat_id": mensaje_id,
            })
            encolados += int(bool(agregado))
        return encolados

    async def procesar_cola(self):
        try:
            while not self.queue.empty():
                tarea = await self.queue.get()
                mensaje_id = tarea.get("mensaje_chat_id")
                resultado = None

                try:
                    datos = (
                        await self._reclamar_registro(mensaje_id)
                        if mensaje_id
                        else tarea
                    )
                    if not datos:
                        continue
                    resultado = await self.service.enviar_mensaje_detallado(
                        telefono=datos["numero"],
                        mensaje=datos["mensaje"],
                        ruta=datos.get("ruta"),
                        mensaje_chat_id=mensaje_id,
                    )

                    if resultado["ok"]:
                        tipo = "PDF + Texto" if datos.get("ruta") else "Texto"
                        logger.info(
                            "✅ Notificación (%s) enviada a %s",
                            tipo,
                            datos["numero"],
                        )
                    else:
                        logger.error(
                            "🚫 Falló envío a %s: %s",
                            datos["numero"],
                            resultado.get("error"),
                        )
                    await self._actualizar_registro(
                        mensaje_id,
                        resultado,
                    )
                except Exception as e:
                    logger.error(f"❌ Fallo crítico en el hilo de la cola: {e}")
                    await self._actualizar_registro(
                        mensaje_id,
                        {
                            "ok": False,
                            "wa_id": None,
                            "error": str(e)[:2000],
                            "reintentable": True,
                            "incierto": False,
                        },
                    )
                finally:
                    self.queue.task_done()
                    if mensaje_id:
                        async with self._processor_lock:
                            self._queued_ids.discard(mensaje_id)

                intervalo_tarea = datos.get(
                    "intervalo",
                    tarea.get("intervalo", 0),
                ) if datos else tarea.get("intervalo", 0)
                wait_time = (
                    intervalo_tarea
                    if intervalo_tarea > 0
                    else GLOBAL_SETTINGS.get("intervalo_default", 60)
                )
                logger.info(
                    "⏳ Control anti-bloqueo: siguiente salida en %s segundos",
                    wait_time,
                )
                await asyncio.sleep(wait_time)
        finally:
            async with self._processor_lock:
                self.is_running = False
                if not self.queue.empty():
                    self.is_running = True
                    asyncio.create_task(self.procesar_cola())

    async def _reclamar_registro(self, mensaje_id: int):
        from sqlalchemy import select
        from src.infrastructure.database import SessionLocal
        from src.infrastructure.models import MensajeChatModel

        ahora = datetime.now()
        async with SessionLocal() as db:
            registro = (
                await db.execute(
                    select(MensajeChatModel)
                    .where(MensajeChatModel.id == mensaje_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if not registro or registro.direccion != "salida":
                return None
            if registro.estado_envio == "eliminado":
                return None
            if registro.estado_envio in ESTADOS_FINALES:
                return None
            if (
                registro.estado_envio == "procesando"
                and registro.bloqueado_hasta
                and registro.bloqueado_hasta > ahora
            ):
                return None
            if registro.intentos >= registro.max_intentos:
                return None

            registro.estado_envio = "procesando"
            registro.intentos += 1
            registro.ultima_tentativa_en = ahora
            registro.bloqueado_hasta = ahora + timedelta(minutes=2)
            registro.proximo_intento_en = None
            await db.commit()
            return {
                "numero": registro.telefono,
                "mensaje": registro.mensaje,
                "ruta": registro.ruta_archivo,
                "intervalo": (
                    registro.intervalo_salida
                    if registro.intervalo_salida and registro.intervalo_salida > 0
                    else GLOBAL_SETTINGS.get("intervalo_default", 60)
                ),
            }

    async def _actualizar_registro(self, mensaje_id, resultado):
        if not mensaje_id:
            return
        from src.infrastructure.database import SessionLocal
        from src.infrastructure.models import MensajeChatModel

        for intento in range(5):
            try:
                async with SessionLocal() as db:
                    registro = await db.get(MensajeChatModel, mensaje_id)
                    if registro and registro.estado_envio != "eliminado":
                        ahora = datetime.now()
                        registro.bloqueado_hasta = None
                        if resultado["ok"]:
                            registro.wa_id = (
                                resultado.get("wa_id") or registro.wa_id
                            )
                            if (
                                registro.ack != -1
                                and (registro.ack or 0) < 1
                            ):
                                registro.ack = 1
                            if registro.estado_envio not in {
                                "entregado",
                                "leido",
                                "fallido",
                            }:
                                registro.estado_envio = "enviado"
                            registro.enviado_en = (
                                registro.enviado_en or ahora
                            )
                            if registro.estado_envio != "fallido":
                                registro.ultimo_error = None
                                registro.proximo_intento_en = None
                        else:
                            registro.ack = -1
                            registro.ultimo_error = resultado.get(
                                "error"
                            ) or "Fallo sin detalle"
                            if resultado.get("incierto"):
                                registro.estado_envio = "incierto"
                                registro.proximo_intento_en = None
                            else:
                                registro.estado_envio = "fallido"
                                if (
                                    resultado.get("reintentable")
                                    and registro.intentos
                                    < registro.max_intentos
                                ):
                                    indice = min(
                                        max(registro.intentos - 1, 0),
                                        len(
                                            RETRASOS_REINTENTO_SEGUNDOS
                                        )
                                        - 1,
                                    )
                                    registro.proximo_intento_en = (
                                        ahora
                                        + timedelta(
                                            seconds=(
                                                RETRASOS_REINTENTO_SEGUNDOS[
                                                    indice
                                                ]
                                            )
                                        )
                                    )
                                else:
                                    registro.proximo_intento_en = None
                        await db.commit()
                        return True
            except Exception as exc:
                logger.error(
                    "No se pudo actualizar la salida %s (intento %s/5): %s",
                    mensaje_id,
                    intento + 1,
                    exc,
                )
            if intento < 4:
                await asyncio.sleep(0.25)
        return False

# Instancia global para ser usada en todo el backend
whatsapp_queue = WhatsAppQueue()
