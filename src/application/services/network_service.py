import logging
import ipaddress 
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select
from fastapi import HTTPException
from sqlalchemy.orm import joinedload

# Modelos y Schemas
from src.infrastructure.models import RouterModel, ClienteModel, RedModel, PlanModel
from src.domain.schemas import RouterCreate, RedCreate
from src.infrastructure.mikrotik_service import MikroTikService

# Configurar logger
logger = logging.getLogger(__name__)

class NetworkService:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ==========================================
    # HELPER PRIVADO (Conexión Lazy)
    # ==========================================
    async def _get_mk_session(self, cliente_id: int):
        stmt = select(ClienteModel).options(joinedload(ClienteModel.router)).where(ClienteModel.id == cliente_id)
        result = await self.db.execute(stmt)
        cliente = result.scalar_one_or_none()

        if not cliente:
            raise HTTPException(status_code=404, detail="Cliente no encontrado")
        if not cliente.router:
            raise HTTPException(status_code=404, detail="Este cliente no tiene un Router asignado")
        
        r = cliente.router
        mk = MikroTikService(r.ip_vpn, r.user_api, r.pass_api, r.port_api)
        return mk, cliente

    # ==========================================
    # 1. GESTIÓN DE ROUTERS
    # ==========================================
    async def crear_router(self, datos: RouterCreate):
        try:
            mk = MikroTikService(datos.ip_vpn, datos.user_api, datos.pass_api, datos.port_api)
            conectado, msg = mk.probar_conexion()
            if not conectado:
                raise ValueError(f"No se pudo establecer conexión con el Router: {msg}")
        except Exception as e:
            if isinstance(e, ValueError): raise e
            logger.error(f"Error conexión Mikrotik: {str(e)}")
            raise ValueError(f"Error técnico al intentar conectar: {str(e)}")

        nuevo_router = RouterModel(**datos.dict())
        self.db.add(nuevo_router)
        try:
            await self.db.commit()
            await self.db.refresh(nuevo_router)
            return nuevo_router
        except Exception as e:
            await self.db.rollback()
            raise ValueError("Error al guardar el router en la base de datos.")

    # ==========================================
    # 2. GESTIÓN DE REDES E IPAM
    # ==========================================
    async def crear_red(self, datos: RedCreate):
        router = await self.db.get(RouterModel, datos.router_id)
        if not router: raise ValueError("El Router especificado no existe.")

        try:
            ipaddress.ip_network(datos.cidr, strict=False)
        except ValueError:
            raise ValueError(f"El formato CIDR '{datos.cidr}' no es válido.")

        nueva_red = RedModel(**datos.dict())
        self.db.add(nueva_red)
        try:
            await self.db.commit()
            await self.db.refresh(nueva_red)
            return nueva_red
        except Exception as e:
            await self.db.rollback()
            raise ValueError(f"Error al crear la red: {str(e)}")

    async def editar_red(self, red_id: int, datos: RedCreate):
        red_db = await self.db.get(RedModel, red_id)
        if not red_db:
            raise ValueError("La red especificada no existe.")

        if datos.router_id != red_db.router_id:
             router = await self.db.get(RouterModel, datos.router_id)
             if not router: raise ValueError("El Router especificado no existe.")

        try:
            ipaddress.ip_network(datos.cidr, strict=False)
        except ValueError:
            raise ValueError(f"El formato CIDR '{datos.cidr}' no es válido.")

        red_db.nombre = datos.nombre
        red_db.cidr = datos.cidr
        red_db.gateway = datos.gateway
        red_db.router_id = datos.router_id

        try:
            await self.db.commit()
            await self.db.refresh(red_db)
            return red_db
        except Exception as e:
            await self.db.rollback()
            raise ValueError(f"Error al editar la red: {str(e)}")

    async def eliminar_red(self, red_id: int):
        stmt = select(func.count(ClienteModel.id)).where(ClienteModel.red_id == red_id)
        result = await self.db.execute(stmt)
        clientes_activos = result.scalar()

        if clientes_activos > 0:
            raise ValueError(f"No se puede eliminar: Hay {clientes_activos} clientes asignados a esta red. Muévelos primero.")

        red_db = await self.db.get(RedModel, red_id)
        if not red_db:
            raise ValueError("La red no existe.")

        await self.db.delete(red_db)
        await self.db.commit()
        
        return "Red eliminada correctamente."

    # ==========================================
    # 3. SINCRONIZACIÓN INTELIGENTE (EL CEREBRO)
    # ==========================================
    async def sincronizar_router(self, router_id: int):
        router = await self.db.get(RouterModel, router_id)
        if not router: raise ValueError("Router no encontrado")

        try:
            mk = MikroTikService(router.ip_vpn, router.user_api, router.pass_api, router.port_api)
            conectado, msg = mk.probar_conexion()
            if not conectado: raise ValueError(f"No hay conexión con el MikroTik: {msg}")
        except Exception as e:
            logger.error(f"Error conexión MK: {e}")
            raise ValueError(f"Error conectando al Router: {str(e)}")

        log_acciones = []

        try:
            # ---------------------------------------------------------
            # PASO A: SINCRONIZAR PLANES (Perfiles PPPoE)
            # ---------------------------------------------------------
            stmt_planes = select(PlanModel).where(PlanModel.router_id == router_id)
            res_planes = await self.db.execute(stmt_planes)
            planes_db = res_planes.scalars().all()
            
            contador_planes = 0
            for plan in planes_db:
                try:
                    subida_k = int(plan.velocidad_subida)
                    bajada_k = int(plan.velocidad_bajada)
                    rate_limit = f"{subida_k}k/{bajada_k}k"
                    
                    # Usamos los nombres correctos de los argumentos
                    mk.crear_actualizar_perfil_pppoe(
                        nombre_plan=plan.nombre,
                        velocidad=rate_limit
                    )
                    contador_planes += 1
                except Exception as e:
                    logger.error(f"Error sincronizando plan {plan.nombre}: {e}")
            
            log_acciones.append(f"{contador_planes} Perfiles PPPoE listos.")

            # ---------------------------------------------------------
            # PASO B: ASEGURAR REGLAS DE CORTE
            # ---------------------------------------------------------
            mk.inicializar_firewall_corte()
            log_acciones.append("Firewall FTTH verificado.")

            # ---------------------------------------------------------
            # PASO C: SINCRONIZAR CLIENTES (Secrets PPPoE)
            # ---------------------------------------------------------
            stmt_clientes = select(ClienteModel).options(joinedload(ClienteModel.plan)).where(ClienteModel.router_id == router_id)
            result = await self.db.execute(stmt_clientes)
            clientes = result.scalars().all()

            contador_clientes = 0

            for cliente in clientes:
                if not cliente.plan or not cliente.user_pppoe: continue

                sn = cliente.cedula if cliente.cedula else "S/N"
                comentario_sync = f"ID:{cliente.id} | {cliente.nombre} | {sn}"

                # 1. Configuración Técnica PPPoE
                mk.crear_actualizar_pppoe(
                    user=cliente.user_pppoe,
                    password=cliente.pass_pppoe,
                    profile=cliente.plan.nombre,
                    remote_address=cliente.ip_asignada,
                    comment=comentario_sync
                )

                # 2. Estado (Corte vs Activo usando la nueva función unificada)
                suspender = True if cliente.estado != 'activo' else False
                mk.gestionar_corte_cliente(cliente.ip_asignada, suspender=suspender)

                contador_clientes += 1
            
            log_acciones.append(f"{contador_clientes} ONUs/Clientes sincronizados.")
            return f"Sincronización Exitosa: {', '.join(log_acciones)}"

        except Exception as e:
            logger.error(f"Error sync router {router_id}: {e}")
            raise ValueError(f"Fallo durante la sincronización: {str(e)}")

    # ==========================================
    # 4. DIAGNÓSTICO EN VIVO Y MONITOREO
    # ==========================================
    async def obtener_estado_tecnico(self, cliente_id: int):
        mk, cliente = await self._get_mk_session(cliente_id)
        
        info = {
            "cliente": cliente.nombre,
            "ip": cliente.ip_asignada,
            "online": False,
            "router_nombre": cliente.router.nombre,
            "tecnologia": "FTTH/PPPoE",
            "consumo": None,
            "detalles": {}
        }

        if cliente.user_pppoe:
            # 1. Verificar sesión activa PPPoE
            sesion = mk.obtener_info_sesion(cliente.user_pppoe)
            if sesion and sesion.get("online"):
                info["online"] = True
                info["detalles"] = {
                    "uptime": sesion.get("uptime"),
                    "mac_onu": sesion.get("mac_onu"),
                    "ip_actual": sesion.get("ip")
                }

            # 2. Obtener consumo real de la interfaz virtual
            consumo = mk.obtener_consumo_interfaz_pppoe(cliente.user_pppoe)
            if consumo:
                info["consumo"] = consumo

        return info

    async def verificar_conexion(self, cliente_id: int):
        mk, cliente = await self._get_mk_session(cliente_id)
        
        online = False
        detalles = {}

        if cliente.user_pppoe:
            sesion = mk.obtener_info_sesion(cliente.user_pppoe)
            if sesion and sesion.get("online"):
                online = True
                detalles = {
                    "uptime": sesion.get("uptime", "0s"),
                    "ip_actual": sesion.get("ip"),
                    "mac_onu": sesion.get("mac_onu")
                }

        return {
            "online": online,
            "metodo": "PPPoE",
            "datos": detalles
        }

    async def verificar_trafico(self, cliente_id: int):
        mk, cliente = await self._get_mk_session(cliente_id)
        if cliente.user_pppoe:
            consumo = mk.obtener_consumo_interfaz_pppoe(cliente.user_pppoe)
            return {
                "velocidad_subida": consumo.get("up_bps", 0),
                "velocidad_bajada": consumo.get("down_bps", 0),
                "total_subida": 0,
                "total_bajada": 0
            }
        return {"velocidad_subida": 0, "velocidad_bajada": 0, "total_subida": 0, "total_bajada": 0}

    async def ping_cliente(self, cliente_id: int):
        mk, cliente = await self._get_mk_session(cliente_id)
        return mk.ping_desde_router(cliente.ip_asignada)