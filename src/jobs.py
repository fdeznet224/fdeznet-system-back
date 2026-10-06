import asyncio
import requests
import os
import time
from datetime import datetime, timedelta
from sqlalchemy import delete, select, func
from sqlalchemy.orm import selectinload
from src.infrastructure.database import SessionLocal
from src.infrastructure.models import (
    ConfiguracionSistema,
    RouterModel,
    ClienteModel,
    LecturaOpticaModel,
    LogCronjobModel,
    MensajeChatModel,
    ServicioModel,
)
from src.infrastructure.mikrotik_service import MikroTikService
from src.infrastructure.whatsapp_client import whatsapp_queue
from src.application.services.billing_service import BillingService
from src.application.services.finance_service import FinanceService
from src.application.services.mikrotik_reconciliation_service import (
    MikrotikReconciliationService,
)
from src.application.services.license_service import verify_license
from src.application.services.router_monitor_service import (
    describir_enlace_vpn,
    evaluar_estado_router,
)
from src.application.services.vpn_service import leer_handshakes_wireguard
from src.application.services.comprobante_service import ComprobanteService
from src.application.services.senal_optica_service import SenalOpticaService, mensaje_alerta
from src.application.services.storage_service import cleanup_storage, close_period, previous_period


async def tarea_verificar_licencia():
    async with SessionLocal() as db:
        await verify_license(db)


# La bitácora de procesos crece unos 7 mil registros al día (monitoreo cada
# minuto, conciliación cada 5). Se guarda lo de los últimos 90 días.
DIAS_BITACORA = 90
# Lecturas automáticas de señal (una por ONU cada hora).
DIAS_LECTURAS_AUTOMATICAS = 60
LOTE_BITACORA = 5000


async def limpiar_bitacora(db, ahora: datetime | None = None) -> int:
    """Borra por lotes la bitácora de procesos más vieja que DIAS_BITACORA."""
    limite = (ahora or datetime.now()) - timedelta(days=DIAS_BITACORA)
    borrados = 0
    while True:
        ids = (
            await db.execute(
                select(LogCronjobModel.id)
                .where(LogCronjobModel.fecha < limite)
                .limit(LOTE_BITACORA)
            )
        ).scalars().all()
        if not ids:
            return borrados
        await db.execute(delete(LogCronjobModel).where(LogCronjobModel.id.in_(ids)))
        await db.commit()
        borrados += len(ids)


async def limpiar_lecturas_automaticas(db, ahora: datetime | None = None) -> int:
    """Las lecturas automáticas viejas; las de técnicos e instalaciones se guardan."""
    limite = (ahora or datetime.now()) - timedelta(days=DIAS_LECTURAS_AUTOMATICAS)
    resultado = await db.execute(
        delete(LecturaOpticaModel).where(
            LecturaOpticaModel.origen == "automatica",
            LecturaOpticaModel.fecha < limite,
        )
    )
    await db.commit()
    return resultado.rowcount or 0


async def tarea_limpiar_bitacora():
    async with SessionLocal() as db:
        await limpiar_lecturas_automaticas(db)
        borrados = await limpiar_bitacora(db)
        if borrados:
            db.add(LogCronjobModel(
                nivel="INFO",
                origen="Mantenimiento",
                mensaje=f"Bitácora: se borraron {borrados} registros de más de {DIAS_BITACORA} días.",
            ))
            await db.commit()


async def leer_senal_optica(db, avisar: bool = False) -> dict:
    """Lee todas las ONU y guarda las lecturas; con avisar, manda las que empeoraron."""
    reporte = await SenalOpticaService(db).tomar_lecturas(evaluar_alertas=avisar)
    texto = mensaje_alerta(reporte) if avisar else None
    if texto:
        await enviar_alertas_whatsapp(texto, db, tipo_evento="alerta_senal")
    resumen = (
        f"Señal óptica: {reporte['leidas']} lecturas, {reporte['sin_senal']} sin señal, "
        f"{len(reporte['alertas'])} empeoraron"
        + (f"; no respondió: {', '.join(reporte['olts_con_error'])}" if reporte["olts_con_error"] else "")
    )
    db.add(LogCronjobModel(
        nivel="WARN" if reporte["olts_con_error"] else "INFO", origen="SenalOptica", mensaje=resumen
    ))
    await db.commit()
    return reporte


# Hora del aviso diario de señal que empeoró (las lecturas son cada hora).
HORA_AVISO_SENAL = 2


async def tarea_leer_senal_optica():
    async with SessionLocal() as db:
        await leer_senal_optica(db, avisar=datetime.now().hour == HORA_AVISO_SENAL)


async def tarea_aplicar_pagos_adelantados():
    """Aplica las capturas válidas de pagos adelantados al llegar su mes."""
    async with SessionLocal() as db:
        await ComprobanteService(db).aplicar_adelantados_por_captura()


async def tarea_mantenimiento_almacenamiento():
    """Ejecuta una vez al día el cierre y la limpieza configurados."""
    async with SessionLocal() as db:
        config = await db.get(ConfiguracionSistema, 1)
        if not config:
            return
        now = datetime.now()
        try:
            hour, minute = (int(value) for value in (config.hora_limpieza_almacenamiento or "02:30").split(":"))
        except (TypeError, ValueError):
            hour, minute = 2, 30
        if (now.hour, now.minute) < (hour, minute):
            return
        already_ran = (
            config.ultima_limpieza_almacenamiento
            and config.ultima_limpieza_almacenamiento.date() == now.date()
        )
        if already_ran:
            return
        try:
            if (
                config.cierre_mensual_automatico
                and now.day >= (config.dia_cierre_almacenamiento or 1)
            ):
                period = previous_period(now)
                await close_period(db, config, period, closure_type="automatico")
            if config.limpieza_almacenamiento_automatica:
                result = await cleanup_storage(db, config)
                db.add(LogCronjobModel(
                    nivel="INFO",
                    origen="Almacenamiento",
                    mensaje=(
                        f"Limpieza automática: {result['bytes_liberados']} bytes liberados; "
                        f"{result['comprobantes_eliminados']} comprobantes y "
                        f"{result['pdf_eliminados']} PDF"
                    ),
                ))
                await db.commit()
            else:
                config.ultima_limpieza_almacenamiento = now
                await db.commit()
        except Exception as exc:
            await db.rollback()
            db.add(LogCronjobModel(
                nivel="ERROR",
                origen="Almacenamiento",
                mensaje=f"Falló el mantenimiento de almacenamiento: {type(exc).__name__}",
            ))
            await db.commit()

# ==========================================
# 📱 NOTIFICACIÓN DE WHATSAPP (Asíncrona)
# ==========================================
async def enviar_alertas_whatsapp(mensaje, db, tipo_evento="alerta_router"):
    try:
        res = await db.execute(select(ConfiguracionSistema).where(ConfiguracionSistema.id == 1))
        config = res.scalar_one_or_none()
        if not config or not config.telefonos_alerta: return
        
        lista_numeros = [n.strip() for n in config.telefonos_alerta.split(",") if n.strip()]
        registros = []
        for numero in lista_numeros:
            registro = MensajeChatModel(
                cliente_id=None,
                telefono=whatsapp_queue.service._formatear_numero(numero),
                direccion="salida",
                mensaje=mensaje,
                tipo_mensaje="texto",
                tipo_evento=tipo_evento,
                leido=True,
                ack=0,
                estado_envio="pendiente",
            )
            db.add(registro)
            registros.append(registro)
        await db.commit()
        for registro in registros:
            await whatsapp_queue.agregar_tarea(
                {"mensaje_chat_id": registro.id, "intervalo": 2}
            )
            
    except Exception as e:
        error_msg = (
            "Fallo al registrar alertas WhatsApp en la bandeja: "
            f"{str(e)}"
        )
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
# Fallas consecutivas por router; se reinicia con el proceso, lo cual sólo
# retrasa unos minutos la siguiente alerta de caída.
_fallas_routers: dict[int, int] = {}


async def tarea_monitoreo_routers():
    print("📡 [RED] Monitoreando estado de routers...")
    async with SessionLocal() as db:
        try:
            routers = (await db.execute(select(RouterModel).where(RouterModel.is_active == True))).scalars().all()
            handshakes = await asyncio.to_thread(leer_handshakes_wireguard)
            for router in routers:
                mk = MikroTikService(router.ip_vpn, router.user_api, router.pass_api, router.port_api)
                respondio, msg = await asyncio.to_thread(mk.probar_conexion)
                conectado, fallas = evaluar_estado_router(
                    bool(router.is_online), respondio, _fallas_routers.get(router.id, 0)
                )
                _fallas_routers[router.id] = fallas
                
                if router.is_online != conectado:
                    router.is_online = conectado
                    estado_texto = "ONLINE ✅" if conectado else "OFFLINE ❌"
                    
                    # 🔥 FORMATO DE FECHA PERSONALIZADO
                    # %d/%m/%Y: día/mes/año, %I:%M:%S %p: 12 horas con am/pm
                    # Usamos .lower().replace() para añadir los puntitos "p.m." / "a.m."
                    fecha_fmt = datetime.now().strftime('%d/%m/%Y, %I:%M:%S %p').lower().replace('pm', 'p.m.').replace('am', 'a.m.')
                    
                    mensaje = f" AVISO Router: {router.nombre} está {estado_texto} el {fecha_fmt}"
                    if not conectado:
                        detalle = describir_enlace_vpn(handshakes.get(router.ip_vpn), time.time())
                        if detalle:
                            mensaje = f"{mensaje}. {detalle}"
                    
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
                    servicios = (
                        await db.execute(
                            select(ServicioModel).where(
                                ServicioModel.router_id == router.id,
                                ServicioModel.estado != "cancelado",
                            )
                        )
                    ).scalars().all()
                    cambios = 0
                    usuarios_online_set = set(usuarios_online)
                    for servicio in servicios:
                        estado_real = (
                            bool(servicio.user_pppoe)
                            and servicio.user_pppoe in usuarios_online_set
                        )
                        if servicio.is_online != estado_real:
                            servicio.is_online = estado_real
                            servicio.ultimo_cambio_estado = datetime.now()
                            cambios += 1
                    
                    await db.commit()
                    db.add(LogCronjobModel(nivel="INFO", origen="Servicios", mensaje=f"Sincronizado '{router.nombre}': {cambios} cambios detectados."))
                    await db.commit()
                else:
                    db.add(LogCronjobModel(nivel="WARN", origen="Servicios", mensaje=f"No se pudo conectar al router '{router.nombre}' para sincronizar."))
                    await db.commit()

            # Compatibilidad: mientras el frontend migra, el estado técnico del
            # cliente representa si al menos uno de sus servicios está online.
            clientes = (
                await db.execute(
                    select(ClienteModel).options(
                        selectinload(ClienteModel.servicios)
                    )
                )
            ).scalars().all()
            for cliente in clientes:
                cliente.is_online = any(
                    servicio.is_online
                    for servicio in cliente.servicios
                    if servicio.estado != "cancelado"
                )
            await db.commit()
        except Exception as e:
            db.add(LogCronjobModel(nivel="ERROR", origen="Servicios", mensaje=f"Error en sincronización: {str(e)}"))
            await db.commit()


# ==========================================
# 2.2 BAJA AUTOMÁTICA POR FALTA DE PAGO
# ==========================================
async def tarea_baja_automatica():
    """Da de baja servicios suspendidos por adeudo más días de lo permitido."""
    async with SessionLocal() as db:
        try:
            config = await db.get(ConfiguracionSistema, 1)
            dias = int(getattr(config, "baja_automatica_dias", 0) or 0)
            if dias <= 0:
                return None
            reporte = await BillingService(db).dar_baja_por_falta_de_pago(dias)
            if reporte["revisados"]:
                db.add(
                    LogCronjobModel(
                        nivel="ERROR" if reporte["errores"] else "INFO",
                        origen="BajaAutomatica",
                        mensaje=(
                            f"Bajas por falta de pago (> {dias} días "
                            f"suspendido): {reporte}"
                        ),
                    )
                )
                await db.commit()
            return reporte
        except Exception as exc:
            await db.rollback()
            db.add(
                LogCronjobModel(
                    nivel="ERROR",
                    origen="BajaAutomatica",
                    mensaje=f"Fallo al procesar bajas automáticas: {exc}",
                )
            )
            await db.commit()
            return None


# ==========================================
# 2.3 CUADRE FACTURA / RENGLONES
# ==========================================
async def tarea_verificar_cuadre_facturas():
    """Recalcula suspendidos y repara renglones que no suman su factura."""
    async with SessionLocal() as db:
        try:
            await BillingService(db).recalcular_suspendidos()
            await db.flush()
            reparadas = await FinanceService(db).verificar_cuadre_facturas()
            for factura_id, diferencia in reparadas:
                db.add(
                    LogCronjobModel(
                        nivel="WARNING",
                        origen="CuadreFacturas",
                        mensaje=(
                            f"Factura #{factura_id}: renglones ajustados "
                            f"{diferencia:+.2f} para cuadrar con su saldo."
                        ),
                    )
                )
            await db.commit()
            return len(reparadas)
        except Exception as exc:
            await db.rollback()
            db.add(
                LogCronjobModel(
                    nivel="ERROR",
                    origen="CuadreFacturas",
                    mensaje=f"Fallo al verificar cuadre de facturas: {exc}",
                )
            )
            await db.commit()
            return 0


# ==========================================
# 2.4 REACTIVAR SERVICIOS YA PAGADOS
# ==========================================
async def tarea_reactivar_servicios_pagados():
    """Reconecta suspendidos por cobranza que ya no deben nada vencido."""
    async with SessionLocal() as db:
        try:
            return await BillingService(db).reactivar_servicios_sin_deuda()
        except Exception as exc:
            await db.rollback()
            db.add(
                LogCronjobModel(
                    nivel="ERROR",
                    origen="ReactivacionPagados",
                    mensaje=f"Fallo al revisar suspendidos pagados: {exc}",
                )
            )
            await db.commit()
            return {"revisados": 0, "reactivados": 0, "errores": 1}


# ==========================================
# 2.5 CONCILIACIÓN BD -> MIKROTIK
# ==========================================
async def tarea_retomar_chats_pausados():
    """El agente vuelve a los chats donde un asesor intervino y ya dejó de escribir."""
    from src.application.services.bot_pausa_service import retomar_chats_pausados
    from src.interfaces.api.whatsapp import lanzar_agente

    async with SessionLocal() as db:
        try:
            return await retomar_chats_pausados(db, lanzar_agente)
        except Exception as exc:
            await db.rollback()
            db.add(LogCronjobModel(
                nivel="ERROR",
                origen="AgenteIA",
                mensaje=f"No se pudieron retomar los chats pausados: {str(exc)[:300]}",
            ))
            await db.commit()
            return 0


async def tarea_conciliar_mikrotik():
    """Repara periódicamente diferencias de activación y suspensión."""
    print("🛡️ [RED] Conciliando estados deseados con MikroTik...")
    async with SessionLocal() as db:
        try:
            return await MikrotikReconciliationService(db).ejecutar()
        except Exception as exc:
            await db.rollback()
            db.add(
                LogCronjobModel(
                    nivel="ERROR",
                    origen="ConciliacionMikroTik",
                    mensaje=(
                        "Fallo fatal del conciliador; se reintentará "
                        f"en el siguiente ciclo: {exc}"
                    ),
                )
            )
            await db.commit()
            return {
                "verificados": 0,
                "correctos": 0,
                "reparados": 0,
                "errores": 1,
                "routers": 0,
            }


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
                    # El día en que vence una promesa de pago.
                    resultado_promesas = await billing_service.enviar_recordatorios_promesa()
                    if resultado_promesas["recordatorios_promesa"]:
                        db.add(LogCronjobModel(
                            nivel="INFO",
                            origen="Recordatorios",
                            mensaje=f"Promesas que vencen hoy: {resultado_promesas}",
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
