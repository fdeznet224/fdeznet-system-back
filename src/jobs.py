# backend/src/jobs.py
import asyncio
import routeros_api
from datetime import datetime
from sqlalchemy import select
from src.infrastructure.database import SessionLocal
from src.infrastructure.models import ConfiguracionSistema, RouterModel, ClienteModel
from src.application.services.billing_service import BillingService

# ==========================================
# ⚡ NUEVO: FUNCIÓN SÍNCRONA AISLADA PARA LA VPN
# ==========================================
def obtener_usuarios_activos_sync(ip, user, password, port):
    """Hace el viaje al MikroTik en un hilo separado para no bloquear la API"""
    try:
        connection = routeros_api.RouterOsApiPool(
            ip, username=user, password=password, port=port, plaintext_login=True
        )
        api = connection.get_api()
        # Traemos la lista de PPPoE activos
        conexiones = api.get_resource('/ppp/active').get()
        # Extraemos solo los nombres de usuario para una búsqueda ultra rápida
        usuarios_online = [c.get('name') for c in conexiones]
        connection.disconnect()
        return usuarios_online
    except Exception as e:
        print(f"⚠️ [RED] Error contactando MikroTik {ip}: {e}")
        return None

# ==========================================
# 🔄 NUEVO: CRONJOB DE ESTADOS (Ejecutar cada 3 min)
# ==========================================
async def tarea_sincronizar_estados_red():
    """Descarga los estados de los routers y actualiza la BD silenciosamente"""
    print("📡 [RED] Iniciando barrido de estados MikroTik...")
    
    async with SessionLocal() as db:
        # 1. Buscar todos los routers activos
        res = await db.execute(select(RouterModel).where(RouterModel.is_active == True))
        routers = res.scalars().all()
        
        for router in routers:
            # 2. Hacer la consulta al MikroTik sin congelar el servidor
            usuarios_online = await asyncio.to_thread(
                obtener_usuarios_activos_sync, 
                router.ip_vpn, router.user_api, router.pass_api, router.port_api
            )
            
            # Si el router no respondió (apagado o error de VPN), saltamos al siguiente
            if usuarios_online is None:
                continue 
                
            # 3. Buscar a los clientes de este router específico
            res_clientes = await db.execute(select(ClienteModel).where(ClienteModel.router_id == router.id))
            clientes = res_clientes.scalars().all()
            
            cambios_detectados = 0
            for cliente in clientes:
                # Validamos si su usuario PPPoE está en la lista que nos dio el router
                estado_real = cliente.user_pppoe in usuarios_online
                
                # Solo escribimos en la base de datos si hubo un cambio de estado
                if cliente.is_online != estado_real:
                    cliente.is_online = estado_real
                    cliente.ultimo_cambio_estado = datetime.now()
                    cambios_detectados += 1
            
            print(f"✅ [RED] Router '{router.nombre}': Sincronizado. {cambios_detectados} cambios de estado.")
            
        # 4. Guardamos todos los cambios de golpe
        await db.commit()


# ==========================================
# 💰 CRONJOB ORIGINAL DE FACTURACIÓN (Ejecutar cada 1 hora)
# ==========================================
async def tarea_cron_unificada():
    """
    Se ejecuta CADA HORA (programado en main.py).
    Revisa en la BD si es el momento de:
    1. Generar Facturas.
    2. Cortar el servicio.
    """
    hora_actual = datetime.now().strftime("%H")
    print(f"⏰ [CRON] Verificando tareas programadas... (Hora servidor: {hora_actual}:00)")

    async with SessionLocal() as db:
        # 1. Leer Configuración del Panel
        res = await db.execute(select(ConfiguracionSistema).where(ConfiguracionSistema.id == 1))
        config = res.scalar_one_or_none()

        if not config:
            print("⚠️ [CRON] No hay configuración en BD. Saltando.")
            return

        # ==========================================
        # A. TAREA DE FACTURACIÓN AUTOMÁTICA
        # ==========================================
        hora_facturacion = getattr(config, 'hora_generacion_facturas', "06:00").split(":")[0]
        
        if hora_actual == hora_facturacion:
            print("🚀 [CRON] Hora de FACTURACIÓN detectada. Iniciando...")
            try:
                billing_service = BillingService(db)
                resultado = await billing_service.generar_emision_masiva()
                print(f"✅ [CRON] Facturación finalizada: {resultado}")
            except Exception as e:
                print(f"❌ [CRON] Error en facturación: {e}")

        # ==========================================
        # B. TAREA DE CORTE AUTOMÁTICO
        # ==========================================
        hora_corte = config.hora_ejecucion_corte.split(":")[0] # Ej: "09"

        if config.activar_corte_automatico and hora_actual == hora_corte:
            print("✂️ [CRON] Hora de CORTE detectada y Switch Activo. Iniciando...")
            try:
                billing_service = BillingService(db)
                resultado = await billing_service.procesar_cortes_automaticos()
                print(f"✅ [CRON] Cortes finalizados. Detalles: {resultado}")
            except Exception as e:
                print(f"❌ [CRON] Error en cortes: {e}")
        
        elif not config.activar_corte_automatico and hora_actual == hora_corte:
            print("⏸️ [CRON] Es hora de corte, pero el sistema está APAGADO en configuración.")