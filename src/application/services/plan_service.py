from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

# Modelos y Schemas
from src.infrastructure.models import PlanModel, RouterModel, ClienteModel, RedModel
from src.domain.schemas import PlanCreate
from src.infrastructure.mikrotik_service import MikroTikService

class PlanService:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ==========================================
    # 1. CREAR PLAN
    # ==========================================
    async def crear_plan(self, datos: PlanCreate):
        # Mapeamos los datos del Schema (Frontend) al Modelo (DB)
        nuevo_plan = PlanModel(
            nombre=datos.nombre,
            precio=datos.precio,
            velocidad_subida=datos.subida_kbps,   
            velocidad_bajada=datos.bajada_kbps,
            
            # Nuevos campos de Calidad de Servicio (QoS)
            garantia_percent=datos.garantia_percent,
            prioridad=datos.prioridad,
            burst_subida=datos.burst_subida,
            burst_bajada=datos.burst_bajada,
            burst_time=datos.burst_time,
            
            router_id=datos.router_id
        )
        
        self.db.add(nuevo_plan)
        await self.db.commit()
        await self.db.refresh(nuevo_plan)
        
        # Sincronizar con MK (Crear Perfil)
        await self._sincronizar_mikrotik(nuevo_plan, "crear")

        return nuevo_plan

    # ==========================================
    # 2. EDITAR PLAN
    # ==========================================
    async def editar_plan(self, plan_id: int, datos: PlanCreate):
        # 1. Buscar Plan
        plan_db = await self.db.get(PlanModel, plan_id)
        if not plan_db:
            raise ValueError("El plan no existe")
        
        nombre_anterior = plan_db.nombre
        
        # 2. Actualizar campos básicos
        plan_db.nombre = datos.nombre
        plan_db.precio = datos.precio
        plan_db.velocidad_subida = datos.subida_kbps
        plan_db.velocidad_bajada = datos.bajada_kbps
        
        # 3. Actualizar campos de QoS
        plan_db.garantia_percent = datos.garantia_percent
        plan_db.prioridad = datos.prioridad
        plan_db.burst_subida = datos.burst_subida
        plan_db.burst_bajada = datos.burst_bajada
        plan_db.burst_time = datos.burst_time
        
        await self.db.commit()
        await self.db.refresh(plan_db)

        # 4. Sincronizar MK (Actualizar)
        await self._sincronizar_mikrotik(plan_db, "editar", nombre_anterior)
        
        return plan_db

    # ==========================================
    # 3. ELIMINAR PLAN
    # ==========================================
    async def eliminar_plan(self, plan_id: int):
        # 1. Validar dependencias (Integridad Referencial)
        stmt = select(func.count(ClienteModel.id)).where(ClienteModel.plan_id == plan_id)
        res = await self.db.execute(stmt)
        clientes_activos = res.scalar()
        
        if clientes_activos > 0:
            raise ValueError(f"No se puede eliminar: Hay {clientes_activos} clientes usando este plan.")

        plan = await self.db.get(PlanModel, plan_id)
        if not plan: 
            raise ValueError("Plan no encontrado")

        # 2. Sincronizar MK (Eliminar perfil)
        await self._sincronizar_mikrotik(plan, "eliminar")

        # 3. Eliminar de BD
        await self.db.delete(plan)
        await self.db.commit()
        
        return f"Plan '{plan.nombre}' eliminado correctamente."

    # ==========================================
    # HELPER DE SINCRONIZACIÓN (CORREGIDO ✅)
    # ==========================================
    
    async def _sincronizar_mikrotik(self, plan: PlanModel, accion: str, nombre_anterior: str = None):
        """
        Gestiona la creación/edición/eliminación de Perfiles PPP en Mikrotik.
        """
        if not plan.router_id: return

        router = await self.db.get(RouterModel, plan.router_id)
        if not router: return
        
        # Solo sincronizamos perfiles si es PPPoE
        tipo_seguridad = str(router.tipo_seguridad).lower()
        if "pppoe" in tipo_seguridad:
            try:
                mk = MikroTikService(router.ip_vpn, router.user_api, router.pass_api, router.port_api)
                
                # 1. Generamos el string de velocidad (Perfecto, no lo toques)
                rate_limit_completo = self._formatear_velocidad_completa(plan)

                # 2. 👇 [IMPORTANTE] BUSCAR EL GATEWAY DE LA RED
                # Buscamos la primera red asociada a este router para saber qué IP (Gateway)
                # ponerle al perfil PPPoE (Local Address).
                stmt_red = select(RedModel).where(RedModel.router_id == router.id)
                red_result = await self.db.execute(stmt_red)
                red = red_result.scalars().first()

                # Si encontramos red y tiene gateway, usamos ese.
                # Si no, usamos la IP del Router como respaldo (Fallback).
                local_addr_ip = red.gateway if (red and red.gateway) else router.ip_vpn
                
                if accion == "crear":
                    # 👇 AQUÍ PASAMOS LA IP DEL GATEWAY
                    mk.crear_actualizar_perfil_pppoe(
                        nombre=plan.nombre, 
                        rate_limit=rate_limit_completo,
                        local_addr=local_addr_ip 
                    )
                    
                elif accion == "editar":
                    # Mismo caso para editar
                    mk.crear_actualizar_perfil_pppoe(
                        nombre=plan.nombre, 
                        rate_limit=rate_limit_completo,
                        local_addr=local_addr_ip
                    )
                    
                elif accion == "eliminar":
                    try:
                        mk.eliminar_perfil_pppoe(plan.nombre)
                    except AttributeError:
                        print("⚠️ Método eliminar_perfil_pppoe no disponible.")

            except Exception as e:
                print(f"⚠️ Error Sincronización MK ({accion}): {e}")

    # ==========================================
    # FORMATEADOR AVANZADO (Lógica ISP Profesional)
    # ==========================================
    def _formatear_velocidad_completa(self, plan: PlanModel):
        """
        Genera el string completo para el campo Rate-Limit de MikroTik.
        Formato: MaxLimit BurstLimit BurstThreshold BurstTime Priority LimitAt
        Ejemplo: 10M/10M 0/0 0/0 0/0 8 10M/10M
        """
        def fmt(val):
            # Convierte 0 -> 0, 10240 -> 10M, 512 -> 512k
            if val == 0: return "0"
            if val >= 1024:
                megas = val / 1024
                # Si es entero (10.0), quitar decimal
                if megas.is_integer():
                    return f"{int(megas)}M"
                return f"{megas:.1f}M"
            return f"{int(val)}k"

        # 1. Max Limit (Velocidad Normal)
        max_limit = f"{fmt(plan.velocidad_subida)}/{fmt(plan.velocidad_bajada)}"

        # 2. Burst Limit, Threshold y Time
        if plan.burst_subida > 0 or plan.burst_bajada > 0:
            burst_limit = f"{fmt(plan.burst_subida)}/{fmt(plan.burst_bajada)}"
            # El Threshold usualmente es igual al Max Limit
            burst_threshold = max_limit 
            time = f"{plan.burst_time}/{plan.burst_time}"
        else:
            burst_limit = "0/0"
            burst_threshold = "0/0"
            time = "0/0"

        # 3. Priority
        prio = str(plan.prioridad)

        # 4. Limit At (Garantía Mínima)
        # Calculamos los kbps garantizados basados en el porcentaje
        garantia_up = int(plan.velocidad_subida * (plan.garantia_percent / 100))
        garantia_down = int(plan.velocidad_bajada * (plan.garantia_percent / 100))
        limit_at = f"{fmt(garantia_up)}/{fmt(garantia_down)}"

        # Concatenar todo el string mágico
        return f"{max_limit} {burst_limit} {burst_threshold} {time} {prio} {limit_at}"