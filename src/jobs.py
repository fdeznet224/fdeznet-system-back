# backend/src/jobs.py
import asyncio
import requests  # 👈 Usamos la librería web estándar en lugar de routeros_api
from datetime import datetime
from sqlalchemy import select
from src.infrastructure.database import SessionLocal
from src.infrastructure.models import ConfiguracionSistema, RouterModel, ClienteModel
from src.application.services.billing_service import BillingService

# ==========================================
# ⚡ FUNCIÓN SÍNCRONA AISLADA PARA PETICIÓN HTTP (PUERTO 80)
# ==========================================
def obtener_usuarios_activos_http_sync(ip, user, password, port=80):
    """
    Hace la petición web al MikroTik por el puerto 80 en un hilo separado.
    Ajusta la URL interna según cómo responde tu script o API en el router.
    """
    try:
        # Ejemplo base: consultando la API web del MikroTik o tu script intermedio
        # Ajusta '/rest/ppp/active' o la ruta exacta que usabas antes si cambiaste el endpoint
        url = f"http://{ip}:{port}/rest/ppp/active" 
        
        # Timeout corto de 5 segundos: si el enlace está inestable o caído, no congela el flujo
        response = requests.get(url, auth=(user, password), timeout=5)
        
        if response.status_code == 200:
            datos = response.json()
            # Extraemos los nombres de los usuarios PPPoE conectados
            # (Asumiendo que el JSON te regresa una lista de objetos con el campo 'name')
            usuarios_online = [usuario.get('name') for usuario in datos if usuario.get('name')]
            return usuarios_online
        else:
            print(f"⚠️ [RED] Router {ip} respondió con código: {response.status_code}")
            return None
            
    except Exception as e:
        print(f"⚠️ [RED] Error HTTP contactando MikroTik {ip}: {e}")
        return None

# ==========================================
# 🔄 CRONJOB DE ESTADOS (Ejecutar cada 3 min)
# ==========================================
async def tarea_sincronizar_estados_red():
    """Descarga los estados de los routers por HTTP y actualiza la BD silenciosamente"""
    print("📡 [RED] Iniciando barrido de estados MikroTik por Web/HTTP...")
    
    async with SessionLocal() as db:
        # 1. Buscar todos los routers activos
        res = await db.execute(select(RouterModel).where(RouterModel.is_active == True))
        routers = res.scalars().all()
        
        for router in routers:
            # 2. Hacemos la petición HTTP sin congelar el servidor FastAPI
            # Pasamos las credenciales y forzamos el puerto de la API web (usualmente 80 o el configurado)
            puerto_web = router.port_api if router.port_api else 80
            
            usuarios_online = await asyncio.to_thread(
                obtener_usuarios_activos_http_sync, 
                router.ip_vpn, router.user_api, router.pass_api, puerto_web
            )
            
            # Si el router no respondió por timeout o error, saltamos al siguiente sin tumbar el script
            if usuarios_online is None:
                continue 
                
            # 3. Buscar a los clientes de este router específico
            res_clientes = await db.execute(select(ClienteModel).where(ClienteModel.router_id == router.id))
            clientes = res_clientes.scalars().all()
            
            cambios_detectados = 0
            for cliente in clientes:
                # Validamos si su usuario PPPoE está en la lista que nos dio el JSON del router
                estado_real = cliente.user_pppoe in usuarios_online
                
                # Solo escribimos en la base de datos si hubo un cambio de estado
                if cliente.is_online != estado_real:
                    cliente.is_online = estado_real
                    cliente.ultimo_cambio_estado = datetime.now()
                    cambios_detectados += 1
            
            print(f"✅ [RED] Router '{router.nombre}': Sincronizado por HTTP. {cambios_detectados} cambios de estado.")
            
        # 4. Guardamos todos los cambios de golpe en MySQL
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