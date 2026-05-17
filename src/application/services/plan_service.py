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
        nuevo_plan = PlanModel(
            nombre=datos.nombre,
            precio=datos.precio,
            velocidad_subida=datos.subida_kbps,   
            velocidad_bajada=datos.bajada_kbps,
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
        
        await self._sincronizar_mikrotik(nuevo_plan, "crear")
        return nuevo_plan

    # ==========================================
    # 2. EDITAR PLAN
    # ==========================================
    async def editar_plan(self, plan_id: int, datos: PlanCreate):
        plan_db = await self.db.get(PlanModel, plan_id)
        if not plan_db:
            raise ValueError("El plan no existe")
        
        # 🔥 GUARDAMOS EL NOMBRE ANTERIOR PARA EL RENOMBRADO
        nombre_viejo = plan_db.nombre
        
        plan_db.nombre = datos.nombre
        plan_db.precio = datos.precio
        plan_db.velocidad_subida = datos.subida_kbps
        plan_db.velocidad_bajada = datos.bajada_kbps
        plan_db.garantia_percent = datos.garantia_percent
        plan_db.prioridad = datos.prioridad
        plan_db.burst_subida = datos.burst_subida
        plan_db.burst_bajada = datos.burst_bajada
        plan_db.burst_time = datos.burst_time
        
        await self.db.commit()
        await self.db.refresh(plan_db)

        # Pasamos el nombre viejo a la sincronización
        await self._sincronizar_mikrotik(plan_db, "editar", nombre_viejo)
        
        return plan_db

    # ==========================================
    # 3. ELIMINAR PLAN
    # ==========================================
    async def eliminar_plan(self, plan_id: int):
        stmt = select(func.count(ClienteModel.id)).where(ClienteModel.plan_id == plan_id)
        res = await self.db.execute(stmt)
        clientes_activos = res.scalar()
        
        if clientes_activos > 0:
            raise ValueError(f"No se puede eliminar: Hay {clientes_activos} clientes usando este plan.")

        plan = await self.db.get(PlanModel, plan_id)
        if not plan: 
            raise ValueError("Plan no encontrado")

        await self._sincronizar_mikrotik(plan, "eliminar")

        await self.db.delete(plan)
        await self.db.commit()
        
        return f"Plan '{plan.nombre}' eliminado correctamente."

    # ==========================================
    # HELPER DE SINCRONIZACIÓN (✅ CORREGIDO)
    # ==========================================
    async def _sincronizar_mikrotik(self, plan: PlanModel, accion: str, nombre_anterior: str = None):
        if not plan.router_id: return

        router = await self.db.get(RouterModel, plan.router_id)
        if not router: return
        
        tipo_seguridad = str(router.tipo_seguridad).lower()
        if "pppoe" in tipo_seguridad:
            try:
                mk = MikroTikService(router.ip_vpn, router.user_api, router.pass_api, router.port_api)
                rate_limit_completo = self._formatear_velocidad_completa(plan)

                stmt_red = select(RedModel).where(RedModel.router_id == router.id)
                red_result = await self.db.execute(stmt_red)
                red = red_result.scalars().first()

                local_addr_ip = red.gateway if (red and red.gateway) else router.ip_vpn
                
                if accion == "crear":
                    # ✅ ENVIAMOS LOS DATOS DIRECTOS (POSICIONALES)
                    mk.crear_actualizar_perfil_pppoe(
                        plan.nombre, 
                        rate_limit_completo,
                        local_addr_ip 
                    )
                    
                elif accion == "editar":
                    if nombre_anterior and nombre_anterior != plan.nombre:
                        try:
                            # 1. Primero renombramos
                            mk.renombrar_perfil_ppp(nombre_anterior, plan.nombre)
                        except Exception as rename_error:
                            print(f"⚠️ Error al renombrar perfil en MK: {rename_error}")

                    # ✅ 2. Luego actualizamos la velocidad con el nuevo nombre (POSICIONAL)
                    mk.crear_actualizar_perfil_pppoe(
                        plan.nombre, 
                        rate_limit_completo,
                        local_addr_ip
                    )
                    
                elif accion == "eliminar":
                    try:
                        mk.eliminar_perfil_pppoe(plan.nombre)
                    except AttributeError:
                        print("⚠️ Método eliminar_perfil_pppoe no disponible.")

            except Exception as e:
                print(f"⚠️ Error Sincronización MK ({accion}): {e}")

    # ==========================================
    # FORMATEADOR AVANZADO
    # ==========================================
    def _formatear_velocidad_completa(self, plan: PlanModel):
        def fmt(val):
            if val == 0: return "0"
            if val >= 1024:
                megas = val / 1024
                if megas.is_integer():
                    return f"{int(megas)}M"
                return f"{megas:.1f}M"
            return f"{int(val)}k"

        max_limit = f"{fmt(plan.velocidad_subida)}/{fmt(plan.velocidad_bajada)}"

        if plan.burst_subida > 0 or plan.burst_bajada > 0:
            burst_limit = f"{fmt(plan.burst_subida)}/{fmt(plan.burst_bajada)}"
            burst_threshold = max_limit 
            time = f"{plan.burst_time}/{plan.burst_time}"
        else:
            burst_limit = "0/0"
            burst_threshold = "0/0"
            time = "0/0"

        prio = str(plan.prioridad)

        garantia_up = int(plan.velocidad_subida * (plan.garantia_percent / 100))
        garantia_down = int(plan.velocidad_bajada * (plan.garantia_percent / 100))
        limit_at = f"{fmt(garantia_up)}/{fmt(garantia_down)}"

        return f"{max_limit} {burst_limit} {burst_threshold} {time} {prio} {limit_at}"