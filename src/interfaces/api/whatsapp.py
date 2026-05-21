import os
import httpx
import logging
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Depends, Request, WebSocket
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.orm import joinedload
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, cast, String

# Importaciones de Infraestructura y Modelos
from src.infrastructure.database import get_db
from src.infrastructure.models import (
    ClienteModel, 
    MensajeChatModel, 
    PagoAutovalidadoModel, 
    FacturaModel, 
    UsuarioModel
)
from src.infrastructure.whatsapp_client import whatsapp_queue
from src.infrastructure.socket_manager import manager

# Importaciones de Servicios
from src.application.services.ocr_service import OCRService
from src.application.services.billing_service import BillingService

router = APIRouter(prefix="/whatsapp", tags=["Configuración WhatsApp"])

# --- CONFIGURACIÓN Y MEMORIA GLOBAL ---
GLOBAL_SETTINGS = {"intervalo_default": 60}
BASE_NODE_URL = "http://whatsapp:3000" if os.environ.get("ENVIRONMENT") == "production" else "http://127.0.0.1:3000"

# Memoria temporal para el Bot (Estado por número de teléfono)
bot_memory = {}
ocr_tool = OCRService()

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

# ==========================================
# ⚙️ ENDPOINTS DE CONFIGURACIÓN Y CAMPAÑAS
# ==========================================
@router.get("/configuracion")
async def get_config(): 
    return GLOBAL_SETTINGS

@router.post("/configuracion")
async def set_config(datos: dict):
    GLOBAL_SETTINGS["intervalo_default"] = datos.get("intervalo_segundos", 60)
    return {"status": "ok", "intervalo": GLOBAL_SETTINGS["intervalo_default"]}

@router.post("/enviar-campana")
async def enviar_campana(datos: CampanaMasiva):
    if not datos.clientes: raise HTTPException(status_code=400, detail="Lista vacía")
    intervalo_final = datos.intervalo_segundos if datos.intervalo_segundos > 0 else GLOBAL_SETTINGS["intervalo_default"]
    count = 0
    for cliente in datos.clientes:
        texto = datos.mensaje.replace("{nombre}", cliente.nombre)
        await whatsapp_queue.agregar_tarea({
            "numero": cliente.numero, 
            "mensaje": texto, 
            "ruta": datos.ruta_archivo, 
            "intervalo": intervalo_final
        })
        count += 1
    return {"status": "procesando", "total_mensajes": count}


# ==========================================
# 🤖 WEBHOOK PRINCIPAL (EL CEREBRO DEL BOT Y CHAT)
# ==========================================
@router.post("/webhook/recibir")
async def webhook_recibir_mensaje(request: Request, db: AsyncSession = Depends(get_db)):
    datos = await request.json()
    
    # 🔥 AHORA RECIBIMOS DOS TELÉFONOS DESDE NODE
    telefono_busqueda = datos.get("telefono", "").strip() # El número real para buscar
    telefono_raw = datos.get("telefono_raw", telefono_busqueda).strip() # El LID para guardar y responder
    
    mensaje_texto = datos.get("mensaje", "").strip()
    media_url = datos.get("mediaUrl")

    if not telefono_raw: return {"status": "ignorado"}
    texto_limpio = mensaje_texto.lower().strip()

    # =========================================================
    # 1. CHAT NORMAL Y GUARDADO EN CRM 
    # =========================================================
    # Extraemos los 10 dígitos del NÚMERO REAL
    numero_base = telefono_busqueda.split('@')[0]
    ultimos_10 = numero_base[-10:] if len(numero_base) >= 10 else numero_base
    
    print("="*40)
    print(f"👉 ID DE WHATSAPP (LID): {telefono_raw}")
    print(f"👉 NÚMERO REAL EXTRAÍDO: {telefono_busqueda}")
    print(f"👉 BUSCANDO EN BD: {ultimos_10}")

    # Forzamos la columna a que se trate como Texto para que el LIKE no falle
    stmt_h = select(ClienteModel).where(cast(ClienteModel.telefono, String).like(f"%{ultimos_10}%"))
    cliente_h = (await db.execute(stmt_h)).scalars().first()

    if cliente_h:
        print(f"✅ ENCONTRADO: {cliente_h.nombre} (ID: {cliente_h.id})")
    else:
        print("❌ NO SE ENCONTRÓ EN LA BD")
    print("="*40)

    # Formateamos cómo se verá en el CRM si mandan una foto/audio
    texto_historial = mensaje_texto
    if media_url:
        if "[FOTO_COMPROBANTE]" in mensaje_texto:
            texto_historial = f"📷 [Imagen enviada] {media_url}"
        else:
            texto_historial = f"📎 [Archivo adjunto] {media_url}"

    # Guardamos en base de datos usando el ID crudo
    nuevo_mensaje = MensajeChatModel(
        cliente_id=cliente_h.id if cliente_h else None,
        telefono=telefono_raw, 
        direccion="entrada",
        mensaje=texto_historial,
        leido=False
    )
    db.add(nuevo_mensaje)
    await db.commit()

    # Avisamos a React (WebSocket)
    await manager.broadcast({
        "type": "NEW_MESSAGE",
        "data": {
            "id": nuevo_mensaje.id,
            "cliente_id": nuevo_mensaje.cliente_id,
            "mensaje": nuevo_mensaje.mensaje,
            "direccion": "entrada"
        }
    })

    # =========================================================
    # 2. ACTIVACIÓN DEL BOT (SOLO SI ESCRIBEN "fdezbot")
    # =========================================================
    if texto_limpio == "fdezbot":
        bot_memory[telefono_raw] = {"paso": "ESPERANDO_OPCION"}
        menu = (
            "🤖 *Bienvenido a FdezNet Bot*\n"
            "Soy tu asistente virtual. Elige una opción:\n\n"
            "1️⃣ *Reportar pago* (Transferencia o Depósito)\n"
            "2️⃣ *Promesa de pago* (Reactivar servicio)\n"
            "3️⃣ *Estado de mi servicio*\n\n"
            "👉 _Responde solo con el número._\n\n"
            "❌ _(Escribe *cancelar* en cualquier momento para salir)_"
        )
        await whatsapp_queue.agregar_tarea({"numero": telefono_raw, "mensaje": menu})
        return {"status": "bot_iniciado"}

    # =========================================================
    # 3. LÓGICA DEL BOT (SOLO SI ESTÁ DESPIERTO PARA ESTE NÚMERO)
    # =========================================================
    if telefono_raw in bot_memory:
        estado = bot_memory[telefono_raw]

        # Comando maestro para apagar el bot
        if texto_limpio == "cancelar":
            del bot_memory[telefono_raw]
            await whatsapp_queue.agregar_tarea({"numero": telefono_raw, "mensaje": "🤖 Asistente desactivado. Un asesor humano te atenderá a la brevedad. ¡Buen día!"})
            return {"status": "bot_apagado"}

        # --- SELECCIÓN DEL MENÚ ---
        if estado["paso"] == "ESPERANDO_OPCION":
            if texto_limpio == "1":
                estado["paso"] = "ESPERANDO_FOTO_PAGO"
                res = "📄 *Reporte de Pago*\nPor favor, envíame la **foto del comprobante** o ticket bien enfocada."
                await whatsapp_queue.agregar_tarea({"numero": telefono_raw, "mensaje": res})
                
            elif texto_limpio == "2":
                estado["paso"] = "VALIDAR_CEDULA_PROMESA"
                res = "⏳ *Promesa de Pago*\nPor favor escribe tu *Cédula de Cliente* para buscar tu cuenta (Ej: 329B)."
                await whatsapp_queue.agregar_tarea({"numero": telefono_raw, "mensaje": res})
                
            elif texto_limpio == "3":
                estado["paso"] = "VALIDAR_CEDULA_ESTADO"
                res = "📊 *Estado del Servicio*\nPor favor, escribe tu *Cédula de Cliente* para buscar tus datos."
                await whatsapp_queue.agregar_tarea({"numero": telefono_raw, "mensaje": res})
                
            else:
                res = "❌ Opción no válida. Por favor, responde 1, 2 o 3. (Escribe 'cancelar' para salir)."
                await whatsapp_queue.agregar_tarea({"numero": telefono_raw, "mensaje": res})
            
            return {"status": "procesando_menu"}

        # --- FLUJO 1: REPORTAR PAGO ---
        elif estado["paso"] == "ESPERANDO_FOTO_PAGO":
            if media_url and "[FOTO_COMPROBANTE]" in mensaje_texto:
                await whatsapp_queue.agregar_tarea({"numero": telefono_raw, "mensaje": "🤖 Analizando tu comprobante... ⏳"})
                
                resultado_ocr = await ocr_tool.procesar_ticket(media_url)
                
                if not resultado_ocr["exito"]:
                    await whatsapp_queue.agregar_tarea({"numero": telefono_raw, "mensaje": "⚠️ No logré leer el folio o monto. Envía una foto más clara o escribe 'cancelar'."})
                    return {"status": "ocr_failed"}

                folio_banco = resultado_ocr["folio"]
                stmt_fraude = select(PagoAutovalidadoModel).where(PagoAutovalidadoModel.folio_banco == folio_banco)
                pago_existente = (await db.execute(stmt_fraude)).scalars().first()

                if pago_existente:
                    res_fraude = f"🚫 *¡Alerta de Seguridad!*\n\nEl comprobante con folio *{folio_banco}* ya fue registrado anteriormente en nuestro sistema. Si crees que es un error, contacta a soporte."
                    del bot_memory[telefono_raw]
                    await whatsapp_queue.agregar_tarea({"numero": telefono_raw, "mensaje": res_fraude})
                    return {"status": "fraude_detectado"}

                if cliente_h:
                    estado["paso"] = "CONFIRMAR_NOMBRE_PAGO"
                    estado["cliente_id"] = cliente_h.id
                    estado["pago"] = resultado_ocr
                    res = f"Detecto que eres *{cliente_h.nombre}*.\n\n¿Deseas aplicar este pago a tu cuenta? (Responde *SI* o *NO*)"
                else:
                    estado["paso"] = "VALIDAR_CEDULA_PAGO"
                    estado["pago"] = resultado_ocr
                    res = f"He leído tu ticket por *${resultado_ocr['monto']}*.\n¿A qué cuenta aplicamos el pago? Escribe la *Cédula de Cliente*."

                await whatsapp_queue.agregar_tarea({"numero": telefono_raw, "mensaje": res})
                return {"status": "bot_init_pago"}
            else:
                await whatsapp_queue.agregar_tarea({"numero": telefono_raw, "mensaje": "⚠️ Estoy esperando una FOTO. (Escribe 'cancelar' para salir). "})
                return {"status": "esperando_foto"}

        elif estado["paso"] == "CONFIRMAR_NOMBRE_PAGO":
            if "si" in texto_limpio:
                estado["paso"] = "VALIDAR_CEDULA_PAGO"
                res = "¡Perfecto! ✅ Escribe tu *Cédula de 4 dígitos* para confirmar la transacción."
            else:
                estado["paso"] = "VALIDAR_CEDULA_PAGO"
                res = "Entendido. Escribe la *Cédula de Cliente* a la que deseas aplicar el pago (Ej. la cuenta de un familiar)."
            await whatsapp_queue.agregar_tarea({"numero": telefono_raw, "mensaje": res})
            return {"status": "bot_confirmando_pago"}

        elif estado["paso"] == "VALIDAR_CEDULA_PAGO":
            cedula_input = mensaje_texto.upper().strip()
            stmt_c = select(ClienteModel).where(ClienteModel.cedula == cedula_input)
            cliente_final = (await db.execute(stmt_c)).scalars().first()

            if cliente_final:
                stmt_f = select(FacturaModel).where(FacturaModel.cliente_id == cliente_final.id, FacturaModel.estado == "pendiente").order_by(FacturaModel.fecha_vencimiento.asc())
                factura = (await db.execute(stmt_f)).scalars().first()

                if not factura:
                    res = "✅ Identidad confirmada, pero no tienes facturas pendientes."
                else:
                    try:
                        stmt_u = select(UsuarioModel).where(UsuarioModel.id == 1)
                        admin_user = (await db.execute(stmt_u)).scalar_one()

                        billing_service = BillingService(db)
                        await billing_service.registrar_pago_completo(factura_id=factura.id, usuario_operador=admin_user, metodo_pago="BOT_AUTOPAGO", monto=estado["pago"]["monto"], referencia=estado["pago"]["folio"])
                        
                        db.add(PagoAutovalidadoModel(cliente_id=cliente_final.id, monto=estado["pago"]["monto"], folio_banco=estado["pago"]["folio"], whatsapp_remitente=telefono_raw))
                        await db.commit()

                        res = f"✅ ¡Todo listo, {cliente_final.nombre}!\nTu pago ha sido procesado exitosamente. Tu internet quedará activo en breve. 🚀"
                    except Exception as e:
                        res = f"❌ Error al procesar el pago: {str(e)}"
                
                del bot_memory[telefono_raw] 
            else:
                res = "❌ Cédula incorrecta. Inténtalo de nuevo o escribe 'cancelar'."
            
            await whatsapp_queue.agregar_tarea({"numero": telefono_raw, "mensaje": res})
            return {"status": "bot_pago_finished"}

        # --- FLUJO 2: PROMESA DE PAGO ---
        elif estado["paso"] == "VALIDAR_CEDULA_PROMESA":
            cedula_input = mensaje_texto.upper().strip()
            stmt_c = select(ClienteModel).where(ClienteModel.cedula == cedula_input)
            cliente_final = (await db.execute(stmt_c)).scalars().first()

            if cliente_final:
                stmt_f = select(FacturaModel).where(FacturaModel.cliente_id == cliente_final.id, FacturaModel.estado == "pendiente").order_by(FacturaModel.fecha_vencimiento.asc())
                factura = (await db.execute(stmt_f)).scalars().first()

                if not factura:
                    res = "✅ No tienes facturas pendientes, tu servicio está al corriente."
                    del bot_memory[telefono_raw]
                elif factura.es_promesa_activa:
                    res = f"⚠️ Ya tienes una promesa de pago activa hasta el {factura.fecha_promesa_pago}. No es posible agregar otra."
                    del bot_memory[telefono_raw]
                else:
                    estado["paso"] = "PEDIR_DIA_PROMESA"
                    estado["cliente_id"] = cliente_final.id
                    estado["factura_id"] = factura.id
                    res = f"Hola {cliente_final.nombre}.\n\n¿Qué día realizarás tu pago?\n👉 Escribe *solo el número* del día (Ejemplo: 15)."
            else:
                res = "❌ Cédula incorrecta. Inténtalo de nuevo o escribe 'cancelar'."
            
            await whatsapp_queue.agregar_tarea({"numero": telefono_raw, "mensaje": res})
            return {"status": "pidiendo_dia_promesa"}

        elif estado["paso"] == "PEDIR_DIA_PROMESA":
            try:
                dia_ingresado = int(texto_limpio)
                if dia_ingresado < 1 or dia_ingresado > 31:
                    raise ValueError()

                hoy = datetime.now().date()
                if dia_ingresado <= hoy.day:
                    mes_objetivo = hoy.month + 1 if hoy.month < 12 else 1
                    ano_objetivo = hoy.year if hoy.month < 12 else hoy.year + 1
                else:
                    mes_objetivo = hoy.month
                    ano_objetivo = hoy.year

                fecha_promesa = datetime(ano_objetivo, mes_objetivo, dia_ingresado).date()
                fecha_con_gracia = fecha_promesa + timedelta(days=1)

                cliente_final = await db.get(ClienteModel, estado["cliente_id"])
                factura = await db.get(FacturaModel, estado["factura_id"])

                factura.fecha_promesa_pago = fecha_con_gracia
                factura.es_promesa_activa = True
                
                if cliente_final.estado == 'suspendido':
                    cliente_final.estado = 'activo'
                    billing_service = BillingService(db)
                    await billing_service._reactivar_en_mikrotik(cliente_final)
                
                await db.commit()
                res = f"✅ ¡Promesa registrada, {cliente_final.nombre}!\n\nTienes hasta el *{fecha_con_gracia.strftime('%d/%m/%Y')}* para realizar tu pago (incluye 1 día de gracia).\nTu servicio ha sido reactivado. 🚀"
                del bot_memory[telefono_raw]

            except ValueError:
                res = "❌ Día no válido. Escribe solamente el número del día (del 1 al 31) o escribe 'cancelar'."

            await whatsapp_queue.agregar_tarea({"numero": telefono_raw, "mensaje": res})
            return {"status": "promesa_finalizada"}

        # --- FLUJO 3: ESTADO DEL SERVICIO ---
        elif estado["paso"] == "VALIDAR_CEDULA_ESTADO":
            cedula_input = mensaje_texto.upper().strip()
            stmt_c = select(ClienteModel).options(joinedload(ClienteModel.plan)).where(ClienteModel.cedula == cedula_input)
            cliente_final = (await db.execute(stmt_c)).scalars().first()

            if cliente_final:
                nombre_plan = cliente_final.plan.nombre if cliente_final.plan else "Sin plan"
                megas_bajada = (cliente_final.plan.velocidad_bajada / 1024) if cliente_final.plan else 0
                estado_str = "🟢 ACTIVO" if cliente_final.estado == "activo" else "🔴 SUSPENDIDO"
                
                res = (
                    f"📊 *ESTADO DE TU SERVICIO*\n\n"
                    f"👤 *Titular:* {cliente_final.nombre}\n"
                    f"📡 *Plan actual:* {nombre_plan} ({int(megas_bajada)} Mbps)\n"
                    f"🌐 *IP:* {cliente_final.ip_asignada or 'Dinámica'}\n"
                    f"🔌 *Estado:* {estado_str}\n"
                    f"💰 *Saldo a favor:* ${cliente_final.saldo_a_favor}\n\n"
                    f"Para volver al menú escribe *fdezbot*."
                )
                del bot_memory[telefono_raw]
            else:
                res = "❌ Cédula incorrecta. Inténtalo de nuevo o escribe 'cancelar'."
            
            await whatsapp_queue.agregar_tarea({"numero": telefono_raw, "mensaje": res})
            return {"status": "bot_estado_finished"}

    return {"status": "chat_normal"}

@router.post("/webhook/ack")
async def webhook_actualizar_ack(data: AckWebhookRequest, db: AsyncSession = Depends(get_db)):
    stmt = select(MensajeChatModel).where(MensajeChatModel.wa_id == data.wa_id)
    mensaje = (await db.execute(stmt)).scalar_one_or_none()
    
    if mensaje:
        mensaje.ack = data.ack
        await db.commit()
        await manager.broadcast({
            "type": "MESSAGE_ACK",
            "data": {"wa_id": data.wa_id, "ack": data.ack, "cliente_id": mensaje.cliente_id}
        })
    return {"status": "ok"}


# ==========================================
# 💬 ENDPOINTS DEL CHAT CRM (REACT)
# ==========================================
@router.get("/no-leidos")
async def obtener_no_leidos(db: AsyncSession = Depends(get_db)):
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
    return {
        str(fila[0]): {"count": fila[1], "antiguedad": fila[2].isoformat() if fila[2] else None} 
        for fila in filas
    }

@router.get("/chat/{cliente_id}")
async def obtener_historial_chat(cliente_id: int, db: AsyncSession = Depends(get_db)):
    stmt = select(MensajeChatModel).where(MensajeChatModel.cliente_id == cliente_id).order_by(MensajeChatModel.fecha.asc())
    mensajes = (await db.execute(stmt)).scalars().all()

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
        cliente_id=cliente.id, telefono=telefono_limpio, direccion="salida", mensaje=data.mensaje, leido=True, ack=0 
    )
    db.add(nuevo_mensaje)
    await db.commit()
    await db.refresh(nuevo_mensaje)

    try:
        async with httpx.AsyncClient() as http_client:
            payload = {"numero": telefono_limpio, "mensaje": data.mensaje}
            resp = await http_client.post(f"{BASE_NODE_URL}/enviar-mensaje", json=payload, timeout=10.0)
            if resp.status_code == 200:
                 datos_respuesta = resp.json()
                 if "wa_id" in datos_respuesta:
                     nuevo_mensaje.wa_id = datos_respuesta["wa_id"]
                     nuevo_mensaje.ack = 1 
                     await db.commit()
    except Exception as e: print(f"❌ Error de conexión Node.js: {e}")
    return {"status": "ok"}

@router.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: str):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except Exception:
        manager.disconnect(websocket)


# ==========================================
# ⚙️ CONTROL DEL MOTOR NODE.JS
# ==========================================
@router.get("/status")
async def obtener_estado():
    """Pregunta a Node.js si el motor está Activo, Conectado y el texto del QR"""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{BASE_NODE_URL}/status", timeout=5.0)
            return resp.json() 
    except Exception as e:
        print(f"⚠️ Error consultando motor Node.js: {e}")
        return {"active": False, "connected": False, "qr": None}

@router.post("/init")
async def iniciar_whatsapp():
    """Manda la orden a Node.js de arrancar el navegador y generar QR"""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{BASE_NODE_URL}/init", timeout=15.0)
            return resp.json()
    except Exception as e:
        print(f"⚠️ Error al iniciar motor: {e}")
        raise HTTPException(status_code=500, detail="No se pudo arrancar el motor de WhatsApp")

@router.post("/logout")
async def logout_whatsapp():
    """Cierra la sesión oficial y apaga el navegador en Node.js"""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{BASE_NODE_URL}/logout", timeout=10.0)
            return resp.json()
    except Exception as e:
        print(f"⚠️ Error al intentar apagar motor: {e}")
        raise HTTPException(status_code=500, detail="Error de comunicación con el motor")