import asyncio
import os
import httpx
from typing import List, Optional
from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.database import get_db
from src.infrastructure.models import ClienteModel, MensajeChatModel
from src.infrastructure.whatsapp_client import whatsapp_queue

router = APIRouter(prefix="/whatsapp", tags=["Configuración WhatsApp"])

GLOBAL_SETTINGS = {"intervalo_default": 60}
WHATSAPP_URL = "http://whatsapp:3000" if os.environ.get("ENVIRONMENT") == "production" else "http://localhost:3000"

# --- SCHEMAS ---
class Destinatario(BaseModel):
    numero: str
    nombre: str

class CampanaMasiva(BaseModel):
    clientes: List[Destinatario]
    mensaje: str
    ruta_archivo: Optional[str] = None
    intervalo_segundos: int = 0  

class MensajeEnviarRequest(BaseModel):
    mensaje: str

class AckWebhookRequest(BaseModel):
    wa_id: str
    ack: int

# --- ENDPOINTS BÁSICOS ---
@router.get("/configuracion")
async def get_config(): return GLOBAL_SETTINGS

@router.post("/configuracion")
async def set_config(datos: dict):
    GLOBAL_SETTINGS["intervalo_default"] = datos.get("intervalo_segundos", 60)
    return {"status": "ok", "intervalo": GLOBAL_SETTINGS["intervalo_default"]}

@router.post("/enviar-campana")
async def enviar_campana(datos: CampanaMasiva):
    # (El código de tu campaña masiva sigue igual...)
    if not datos.clientes: raise HTTPException(status_code=400, detail="Lista vacía")
    intervalo_final = datos.intervalo_segundos if datos.intervalo_segundos > 0 else GLOBAL_SETTINGS["intervalo_default"]
    count = 0
    for cliente in datos.clientes:
        texto = datos.mensaje.replace("{nombre}", cliente.nombre)
        await whatsapp_queue.agregar_tarea({"numero": cliente.numero, "mensaje": texto, "ruta": datos.ruta_archivo, "intervalo": intervalo_final})
        count += 1
    return {"status": "procesando", "total_mensajes": count}

@router.get("/status")
async def obtener_estado():
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{WHATSAPP_URL}/status", timeout=5.0)
            return resp.json()
    except Exception: return {"connected": False}

# --- WEBHOOKS (NODE.JS -> PYTHON) ---
@router.post("/webhook/recibir")
async def webhook_recibir_mensaje(request: Request, db: AsyncSession = Depends(get_db)):
    datos = await request.json()
    telefono_entrante = datos.get("telefono", "").replace("@c.us", "").strip()
    mensaje_texto = datos.get("mensaje", "")
    if not telefono_entrante or not mensaje_texto: return {"status": "ignorado"}

    ultimos_10 = telefono_entrante[-10:] if len(telefono_entrante) >= 10 else telefono_entrante
    stmt = select(ClienteModel).where(ClienteModel.telefono.like(f"%{ultimos_10}%"))
    cliente = (await db.execute(stmt)).scalar_one_or_none()

    nuevo_mensaje = MensajeChatModel(
        cliente_id=cliente.id if cliente else None,
        telefono=telefono_entrante,
        direccion="entrada",
        mensaje=mensaje_texto,
        leido=False
    )
    db.add(nuevo_mensaje)
    await db.commit()
    return {"status": "ok"}

@router.post("/webhook/ack")
async def webhook_actualizar_ack(data: AckWebhookRequest, db: AsyncSession = Depends(get_db)):
    """Actualiza si el mensaje fue Enviado (1), Entregado (2) o Leído (3)"""
    stmt = select(MensajeChatModel).where(MensajeChatModel.wa_id == data.wa_id)
    mensaje = (await db.execute(stmt)).scalar_one_or_none()
    
    if mensaje:
        mensaje.ack = data.ack
        await db.commit()
    return {"status": "ok"}

# --- CHAT CRM (REACT -> PYTHON) ---

@router.get("/no-leidos")
async def obtener_no_leidos(db: AsyncSession = Depends(get_db)):
    # Buscamos el conteo y la fecha del mensaje más antiguo sin leer
    stmt = select(
        MensajeChatModel.cliente_id, 
        func.count(MensajeChatModel.id),
        func.min(MensajeChatModel.fecha) 
    ).where(
        MensajeChatModel.direccion == 'entrada',
        MensajeChatModel.leido == False,
        MensajeChatModel.cliente_id.isnot(None)
    ).group_by(MensajeChatModel.cliente_id)
    
    filas = (await db.execute(stmt)).all()
    # Enviamos el objeto con 'count' para que el Front pueda calcular el total
    return {
        str(fila[0]): {"count": fila[1], "antiguedad": fila[2].isoformat() if fila[2] else None} 
        for fila in filas
    }

@router.get("/chat/{cliente_id}")
async def obtener_historial_chat(cliente_id: int, db: AsyncSession = Depends(get_db)):
    stmt = select(MensajeChatModel).where(MensajeChatModel.cliente_id == cliente_id).order_by(MensajeChatModel.fecha.asc())
    mensajes = (await db.execute(stmt)).scalars().all()

    # Marcar como leídos al abrir
    mensajes_no_leidos = [m for m in mensajes if m.direccion == 'entrada' and not m.leido]
    if mensajes_no_leidos:
        for m in mensajes_no_leidos: m.leido = True
        await db.commit()
    return mensajes

@router.post("/chat/{cliente_id}/enviar")
async def enviar_mensaje_chat(cliente_id: int, data: MensajeEnviarRequest, db: AsyncSession = Depends(get_db)):
    cliente = await db.get(ClienteModel, cliente_id)
    if not cliente or not cliente.telefono: raise HTTPException(status_code=404, detail="Cliente no encontrado")

    telefono_limpio = cliente.telefono.replace("+", "").replace(" ", "")
    if len(telefono_limpio) == 10: telefono_limpio = f"521{telefono_limpio}"
    elif len(telefono_limpio) == 12 and telefono_limpio.startswith("52"): telefono_limpio = f"521{telefono_limpio[2:]}"

    nuevo_mensaje = MensajeChatModel(
        cliente_id=cliente.id,
        telefono=telefono_limpio,
        direccion="salida",
        mensaje=data.mensaje,
        leido=True,
        ack=0 # Inicia en Pendiente
    )
    db.add(nuevo_mensaje)
    await db.commit()
    await db.refresh(nuevo_mensaje)

    # Disparar a Node.js
    try:
        async with httpx.AsyncClient() as http_client:
            payload = {"numero": telefono_limpio, "mensaje": data.mensaje}
            resp = await http_client.post(f"{WHATSAPP_URL}/enviar-mensaje", json=payload, timeout=10.0)
            
            if resp.status_code == 200:
                 datos_respuesta = resp.json()
                 if "wa_id" in datos_respuesta:
                     nuevo_mensaje.wa_id = datos_respuesta["wa_id"]
                     nuevo_mensaje.ack = 1 # Cambia a Enviado
                     await db.commit()
    except Exception as e:
        print(f"❌ Error de conexión Node.js: {e}")

    return {"status": "ok"}