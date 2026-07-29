import asyncio
import requests
import os
from datetime import datetime, timedelta
from sqlalchemy import select, func
from src.infrastructure.database import SessionLocal
from src.infrastructure.models import ConfiguracionSistema, RouterModel, ClienteModel, LogCronjobModel
from src.infrastructure.mikrotik_service import MikroTikService
from src.application.services.billing_service import BillingService

# ==========================================
# 📱 NOTIFICACIÓN DE WHATSAPP (Asíncrona)
# ==========================================
async def enviar_alertas_whatsapp(mensaje, db):
    numero = "desconocido"
    try:
        res = await db.execute(select(ConfiguracionSistema).where(ConfiguracionSistema.id == 1))
        config = res.scalar_one_or_none()
        if not config or not config.telefonos_alerta: return
        
        lista_numeros = [n.strip() for n in config.telefonos_alerta.split(",") if n.strip()]
        for numero in lista_numeros:
            # Hacemos la petición al bot local
            requests.post(
                os.getenv("WHATSAPP_BASE_URL", "http://127.0.0.1:3000")
                + "/enviar-mensaje",
                json={"numero": numero, "mensaje": mensaje},
                headers={"X-Webhook-Secret": os.getenv("WEBHOOK_SECRET", "")},
                timeout=5,
            )
            
    except Exception as e:
        error_msg = f"Fallo al enviar WhatsApp al {numero}: El bot no responde o está apagado. Detalle: {str(e)}"
        print(f"❌ {error_msg}")
        
        # Guardar en la base de datos para verlo en el panel
        db.add(LogCronjobModel(
            nivel="ERROR", 
            origen="WhatsAppBot", 
            mensaje=error_msg
        ))
        await db.commit()

# ==========================================
# ⚡ FUNCIÓN SÍNCRONA HTTP (Clientes)
# ==========================================
def obtener_usuarios_activos_http_sync(ip, user, password, port=80):
    try:
        url = f"http://{ip}:{port}/rest/ppp/active" 
        response = requests.get(url, auth=(user, password), timeout=5)
        if response.status_code == 200:
            return [usuario.get('name') for usuario in response.json() if usuario.get('name')]
        return None
    except: return None

# ==========================================
# 1. TAREA DE MONITOREO DE ROUTERS (PING)
# ==========================================
async def tarea_monitoreo_routers():
    print("📡 [RED] Monitoreando estado de routers...")
    async with SessionLocal() as db:
        try:
            routers = (await db.execute(select(RouterModel).where(RouterModel.is_active == True))).scalars().all()
            for router in routers:
                mk = MikroTikService(router.ip_vpn, router.user_api, router.pass_api, router.port_api)
                conectado, msg = await asyncio.to_thread(mk.probar_conexion)
                
                if router.is_online != conectado:
                    router.is_online = conectado
                    estado_texto = "ONLINE ✅" if conectado else "OFFLINE ❌"
                    
                    # 🔥 FORMATO DE FECHA PERSONALIZADO
                    # %d/%m/%Y: día/mes/año, %I:%M:%S %p: 12 horas con am/pm
                    # Usamos .lower().replace() para añadir los puntitos "p.m." / "a.m."
                    fecha_fmt = datetime.now().strftime('%d/%m/%Y, %I:%M:%S %p').lower().replace('pm', 'p.m.').replace('am', 'a.m.')
                    
                    mensaje = f" AVISO Router: {router.nombre} está {estado_texto} el {fecha_fmt}"
                    
                    # Notificar y Loguear
                    await enviar_alertas_whatsapp(mensaje, db)
                    db.add(LogCronjobModel(
                        nivel="WARN" if not conectado else "INFO", 
                        origen="Red", 
                        mensaje=mensaje
                    ))
                    
                    await db.commit()
            
            # Log de control (Heartbeat)
            db.add(LogCronjobModel(nivel="INFO", origen="Red", mensaje="Monitoreo de routers completado."))
            await db.commit()
        except Exception as e:
            db.add(LogCronjobModel(nivel="ERROR", origen="Red", mensaje=f"Error fatal en monitoreo: {str(e)}"))
            await db.commit()

# ==========================================
# 2. TAREA DE SINCRONIZACIÓN DE CLIENTES
# ==========================================
async def tarea_sincronizar_clientes():
    print("🔄 [RED] Sincronizando clientes...")
    async with SessionLocal() as db:
        try:
            routers = (await db.execute(select(RouterModel).where(RouterModel.is_active == True))).scalars().all()
            for router in routers:
                usuarios_online = await asyncio.to_thread(obtener_usuarios_activos_http_sync, router.ip_vpn, router.user_api, router.pass_api, (router.port_api or 80))
                
                if usuarios_online is not None:
                    clientes = (await db.execute(select(ClienteModel).where(ClienteModel.router_id == router.id))).scalars().all()
                    cambios = 0
                    for cliente in clientes:
                        estado_real = cliente.user_pppoe in usuarios_online
                        if cliente.is_online != estado_real:
                            cliente.is_online = estado_real
                            cliente.ultimo_cambio_estado = datetime.now()
                            cambios += 1
                    
                    await db.commit()
                    db.add(LogCronjobModel(nivel="INFO", origen="Clientes", mensaje=f"Sincronizado '{router.nombre}': {cambios} cambios detectados."))
                    await db.commit()
                else:
                    db.add(LogCronjobModel(nivel="WARN", origen="Clientes", mensaje=f"No se pudo conectar al router '{router.nombre}' para sincronizar."))
                    await db.commit()
        except Exception as e:
            db.add(LogCronjobModel(nivel="ERROR", origen="Clientes", mensaje=f"Error en sincronización: {str(e)}"))
            await db.commit()

# ==========================================
# 3. TAREA DE FACTURACIÓN Y CORTES
# ==========================================

# FACTURACION_ISP_V2_CUT_CRON_RECOVERY
async def _corte_automatico_ejecutado_hoy(db) -> bool:
    inicio = datetime.combine(
        datetime.now().date(),
        datetime.min.time(),
    )
    fin = inicio + timedelta(days=1)

    stmt = select(func.count(LogCronjobModel.id)).where(
        LogCronjobModel.origen == "CortesAutomaticos",
        LogCronjobModel.fecha >= inicio,
        LogCronjobModel.fecha < fin,
    )
    cantidad = (
        await db.execute(stmt)
    ).scalar_one()

    return cantidad > 0


async def tarea_cron_unificada():
    # Compara hora y minuto para respetar configuraciones como 06:30.
    momento_actual = datetime.now().strftime("%H:%M")
    
    async with SessionLocal() as db:
        try:
            config = (await db.execute(select(ConfiguracionSistema).where(ConfiguracionSistema.id == 1))).scalar_one_or_none()
            if not config: return
            billing_service = BillingService(db)

            # Extraemos limpiamente solo el componente de la hora (de "03:00" nos deja "03")
            hora_corte = (config.hora_ejecucion_corte or "03:00")[:5]
            hora_facturas = (config.hora_generacion_facturas or "06:00")[:5]
            hora_mensajes = (config.hora_recordatorios or "09:00")[:5]

            # ------------------------------------------------------
            # A. GENERACIÓN DE FACTURAS AUTOMÁTICA (5 días antes)
            # ------------------------------------------------------
            if config.generar_facturas_automaticamente:
                if momento_actual == hora_facturas:
                    resultado = await billing_service.generar_emision_masiva()
                    db.add(LogCronjobModel(
                        nivel="INFO", 
                        origen="Facturación", 
                        mensaje=f"Emisión Masiva Ejecutada: {resultado}"
                    ))

            # ------------------------------------------------------
            # B. RECORDATORIO DE PAGO URGENTE (1 día antes)
            # ------------------------------------------------------
            if config.activar_notificaciones:
                if momento_actual == hora_mensajes:
                    # Lee directamente tu nueva columna de la BD
                    dias_urgente = config.recordatorio_2_dias or 0
                    
                    # Ejecuta sólo si el interruptor no es 0
                    if dias_urgente > 0:
                        resultado_rec = await billing_service.enviar_recordatorios_automaticos(dias_aviso_urgente=dias_urgente)
                        db.add(LogCronjobModel(
                            nivel="INFO", 
                            origen="Recordatorios", 
                            mensaje=f"Recordatorios Enviados ({dias_urgente} días antes): {resultado_rec}"
                        ))

            # ------------------------------------------------------
            # C. MOTOR DE CORTES AUTOMÁTICOS (Fecha límite superada)
            # ------------------------------------------------------
            if config.activar_corte_automatico:
                hora_programada = int(hora_corte.split(":")[0])
                hora_servidor = int(momento_actual.split(":")[0])
                if (
                    hora_servidor >= hora_programada
                    and not await _corte_automatico_ejecutado_hoy(db)
                ):
                    resultado_corte = (
                        await billing_service.procesar_cortes_automaticos()
                    )
                    db.add(
                        LogCronjobModel(
                            nivel="INFO",
                            origen="CortesAutomaticos",
                            mensaje=(
                                "Cortes del día procesados: "
                                f"{resultado_corte}"
                            ),
                        )
                    )
            
            await db.commit()
            
        except Exception as e:
            db.add(LogCronjobModel(
                nivel="ERROR", 
                origen="Sistema", 
                mensaje=f"Error fatal en el ciclo del Cronjob: {str(e)}"
            ))
            await db.commit()
