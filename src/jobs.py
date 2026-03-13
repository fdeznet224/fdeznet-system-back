# backend/src/jobs.py
import asyncio
from datetime import datetime
from sqlalchemy import select
from src.infrastructure.database import SessionLocal
from src.infrastructure.models import ConfiguracionSistema
from src.application.services.billing_service import BillingService

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
                # 🚀 CORRECCIÓN: Usamos BillingService porque él es quien lee las facturas
                billing_service = BillingService(db)
                resultado = await billing_service.procesar_cortes_automaticos()
                print(f"✅ [CRON] Cortes finalizados. Detalles: {resultado}")
            except Exception as e:
                print(f"❌ [CRON] Error en cortes: {e}")
        
        elif not config.activar_corte_automatico and hora_actual == hora_corte:
            print("⏸️ [CRON] Es hora de corte, pero el sistema está APAGADO en configuración.")