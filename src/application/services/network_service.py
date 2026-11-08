import logging
import ipaddress 
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select
from fastapi import HTTPException
from sqlalchemy.orm import joinedload

# Modelos y Schemas
from src.infrastructure.models import (
    RouterModel,
    ClienteModel,
    RedModel,
    PlanModel,
    ServicioModel,
)
from src.domain.schemas import RouterCreate, RedCreate
from src.application.services.corte_whatsapp_service import (
    aplicar_modo_corte,
    obtener_modo_corte,
)
from src.infrastructure.mikrotik_service import MikroTikService
from src.application.services.acceso_red import NOMBRES as NOMBRES_MODO, modo_router
from src.utils.mikrotik import (
    debe_bloquear_acceso,
    formatear_rate_limit_dhcp,
    formatear_rate_limit_pppoe,
    normalizar_mac,
)

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

    async def _get_mk_session_servicio(self, servicio_id: int):
        stmt = (
            select(ServicioModel)
            .options(
                joinedload(ServicioModel.router),
                joinedload(ServicioModel.cliente),
            )
            .where(ServicioModel.id == servicio_id)
        )
        servicio = (await self.db.execute(stmt)).scalar_one_or_none()
        if not servicio:
            raise HTTPException(
                status_code=404,
                detail="Servicio no encontrado",
            )
        if not servicio.router:
            raise HTTPException(
                status_code=404,
                detail="El servicio no tiene un router asignado",
            )
        router = servicio.router
        mk = MikroTikService(
            router.ip_vpn,
            router.user_api,
            router.pass_api,
            router.port_api,
        )
        return mk, servicio

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
        stmt = select(func.count(ServicioModel.id)).where(
            ServicioModel.red_id == red_id,
            ServicioModel.estado != "cancelado",
        )
        result = await self.db.execute(stmt)
        servicios_activos = result.scalar()

        if servicios_activos > 0:
            raise ValueError(
                "No se puede eliminar: Hay "
                f"{servicios_activos} servicios asignados a esta red. "
                "Muévelos primero."
            )

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
        from src.application.services.acceso_red import modo_router

        # Sin PPPoE en el router (DHCP o IP fija) no hacen falta perfiles.
        es_dhcp = modo_router(router) in ("dhcp", "estatica")

        try:
            # ---------------------------------------------------------
            # PASO A: SINCRONIZAR PLANES (Perfiles PPPoE)
            # ---------------------------------------------------------
            stmt_planes = select(PlanModel).where(PlanModel.router_id == router_id)
            res_planes = await self.db.execute(stmt_planes)
            planes_db = res_planes.scalars().all()
            
            contador_planes = 0
            for plan in planes_db:
                if es_dhcp:
                    continue
                try:
                    rate_limit = formatear_rate_limit_pppoe(plan)
                    
                    # Usamos los nombres correctos de los argumentos
                    mk.crear_actualizar_perfil_pppoe(
                        nombre_plan=plan.nombre,
                        velocidad=rate_limit
                    )
                    contador_planes += 1
                except Exception as e:
                    logger.error(f"Error sincronizando plan {plan.nombre}: {e}")
            
            log_acciones.append(
                "DHCP e IP fija llevan la velocidad en cada cliente."
                if es_dhcp
                else f"{contador_planes} Perfiles PPPoE listos."
            )

            # ---------------------------------------------------------
            # PASO B: ASEGURAR REGLAS DE CORTE
            # ---------------------------------------------------------
            modo_corte = await obtener_modo_corte(self.db)
            firewall_ok, firewall_msg = aplicar_modo_corte(mk, modo_corte)
            log_acciones.append(
                f"{firewall_msg}."
                if firewall_ok
                else f"Firewall de corte con error ({firewall_msg})."
            )

            # ---------------------------------------------------------
            # PASO C: SINCRONIZAR CLIENTES (Secrets PPPoE)
            # ---------------------------------------------------------
            stmt_servicios = (
                select(ServicioModel)
                .options(
                    joinedload(ServicioModel.plan),
                    joinedload(ServicioModel.cliente),
                )
                .where(
                    ServicioModel.router_id == router_id,
                    ServicioModel.estado != "cancelado",
                )
            )
            result = await self.db.execute(stmt_servicios)
            servicios = result.scalars().all()

            contador_servicios = 0

            for servicio in servicios:
                if not servicio.plan:
                    continue

                cliente = servicio.cliente
                cedula_str = cliente.cedula if cliente.cedula else "S/A"
                comentario_estandar = (
                    f"{cliente.nombre} | Servicio:{servicio.id} "
                    f"{servicio.alias} | SN:{cedula_str}"
                )

                if es_dhcp:
                    mac = normalizar_mac(servicio.mac_address)
                    if not mac or not servicio.ip_asignada:
                        continue
                    mk.crear_actualizar_lease_dhcp(
                        mac,
                        servicio.ip_asignada,
                        formatear_rate_limit_dhcp(servicio.plan),
                        comentario_estandar,
                    )
                    mk.activar_desactivar_dhcp(
                        mac,
                        blocked=(
                            servicio.estado not in {"activo", "suspendido"}
                            or debe_bloquear_acceso(
                                servicio.estado == "suspendido",
                                modo_corte.solo_whatsapp,
                            )
                        ),
                    )
                else:
                    if not servicio.user_pppoe:
                        continue
                    mk.crear_actualizar_pppoe(
                        user=servicio.user_pppoe,
                        password=servicio.pass_pppoe,
                        profile=servicio.plan.nombre,
                        remote_address=servicio.ip_asignada,
                        comment=comentario_estandar
                    )

                # 2. Estado (Corte vs Activo usando la nueva función unificada)
                suspender = servicio.estado != "activo"
                mk.gestionar_corte_cliente(
                    servicio.ip_asignada,
                    suspender=suspender,
                )

                contador_servicios += 1
            
            log_acciones.append(
                f"{contador_servicios} servicios sincronizados."
            )
            return f"Sincronización Exitosa: {', '.join(log_acciones)}"

        except Exception as e:
            logger.error(f"Error sync router {router_id}: {e}")
            raise ValueError(f"Fallo durante la sincronización: {str(e)}")

    # ==========================================
    # 4. DIAGNÓSTICO EN VIVO Y MONITOREO
    # ==========================================
    @staticmethod
    def _estado_en_vivo(mk, router, datos) -> dict:
        """En línea según el modo del router: sesión PPPoE, o ping/ARP por IP."""
        modo = modo_router(router)
        if modo == "pppoe":
            sesion = mk.obtener_info_sesion(datos.user_pppoe) if datos.user_pppoe else None
            if sesion and sesion.get("online"):
                return {"online": True, "metodo": "PPPoE", "datos": {
                    "uptime": sesion.get("uptime", "0s"),
                    "ip_actual": sesion.get("ip"),
                    "mac_onu": sesion.get("mac_onu"),
                }}
            return {"online": False, "metodo": "PPPoE", "datos": {}}
        resultado = mk.estado_por_ip(
            datos.ip_asignada,
            mac=getattr(datos, "mac_address", None) if modo == "dhcp" else None,
        )
        return {"online": resultado["online"], "metodo": NOMBRES_MODO[modo], "datos": resultado["datos"]}

    @staticmethod
    def _trafico_en_vivo(mk, router, datos) -> dict:
        if modo_router(router) == "pppoe":
            if not datos.user_pppoe:
                return {}
            return mk.obtener_consumo_interfaz_pppoe(datos.user_pppoe) or {}
        return mk.trafico_por_ip(datos.ip_asignada)

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

        estado = self._estado_en_vivo(mk, cliente.router, cliente)
        info["tecnologia"] = estado["metodo"]
        info["online"] = estado["online"]
        if estado["online"]:
            info["detalles"] = estado["datos"]
        consumo = self._trafico_en_vivo(mk, cliente.router, cliente)
        if consumo:
            info["consumo"] = consumo

        return info

    async def verificar_conexion(self, cliente_id: int):
        mk, cliente = await self._get_mk_session(cliente_id)
        return self._estado_en_vivo(mk, cliente.router, cliente)

    async def verificar_trafico(self, cliente_id: int):
        mk, cliente = await self._get_mk_session(cliente_id)
        consumo = self._trafico_en_vivo(mk, cliente.router, cliente)
        return {
            "velocidad_subida": consumo.get("up_bps", 0),
            "velocidad_bajada": consumo.get("down_bps", 0),
            "total_subida": 0,
            "total_bajada": 0
        }

    async def ping_cliente(self, cliente_id: int):
        mk, cliente = await self._get_mk_session(cliente_id)
        return mk.ping_desde_router(cliente.ip_asignada)

    async def obtener_estado_tecnico_servicio(self, servicio_id: int):
        mk, servicio = await self._get_mk_session_servicio(servicio_id)
        info = {
            "cliente": servicio.cliente.nombre,
            "cliente_id": servicio.cliente_id,
            "servicio_id": servicio.id,
            "servicio": servicio.alias,
            "direccion": servicio.direccion,
            "ip": servicio.ip_asignada,
            "online": False,
            "router_nombre": servicio.router.nombre,
            "tecnologia": "FTTH/PPPoE",
            "consumo": None,
            "detalles": {},
        }
        estado = self._estado_en_vivo(mk, servicio.router, servicio)
        info["tecnologia"] = estado["metodo"]
        info["online"] = estado["online"]
        if estado["online"]:
            info["detalles"] = estado["datos"]
        consumo = self._trafico_en_vivo(mk, servicio.router, servicio)
        if consumo:
            info["consumo"] = consumo
        return info

    async def verificar_conexion_servicio(self, servicio_id: int):
        mk, servicio = await self._get_mk_session_servicio(servicio_id)
        return {"servicio_id": servicio.id, **self._estado_en_vivo(mk, servicio.router, servicio)}

    async def verificar_trafico_servicio(self, servicio_id: int):
        mk, servicio = await self._get_mk_session_servicio(servicio_id)
        consumo = self._trafico_en_vivo(mk, servicio.router, servicio)
        return {
            "servicio_id": servicio.id,
            "velocidad_subida": consumo.get("up_bps", 0),
            "velocidad_bajada": consumo.get("down_bps", 0),
            "total_subida": 0,
            "total_bajada": 0,
        }

    async def ping_servicio(self, servicio_id: int):
        mk, servicio = await self._get_mk_session_servicio(servicio_id)
        if not servicio.ip_asignada:
            raise ValueError("El servicio no tiene IP asignada")
        return mk.ping_desde_router(servicio.ip_asignada)
