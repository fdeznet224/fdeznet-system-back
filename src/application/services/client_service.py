from datetime import date, timedelta, datetime
import random
import string
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, text, and_
from sqlalchemy.orm import joinedload, selectinload 
from fastapi import BackgroundTasks, HTTPException

# Base de datos 
from src.infrastructure.database import SessionLocal as async_session 

# Modelos y Schemas
from src.infrastructure.models import ClienteModel, PagoModel, RouterModel, FacturaModel, CajaNapModel 
from src.domain.schemas import ClienteCreate
from src.infrastructure.repositories import ClienteRepository

# Servicios Externos
from src.infrastructure.mikrotik_service import MikroTikService
from src.application.services.notification_service import NotificationService

class ClientService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = ClienteRepository(db)

    # ==========================================
    # 0. MÉTODOS DE BÚSQUEDA
    # ==========================================
    async def get_cliente_by_id(self, id: int):
        """Busca un cliente por su ID."""
        return await self.repo.get_by_id(id)

    # ==========================================
    # 1. REGISTRAR CLIENTE (PASO 1: CREAR ORDEN)
    # ==========================================
    async def registrar_cliente(self, datos: ClienteCreate, background_tasks: BackgroundTasks):
        """
        Crea la orden en BD. 
        - Evita duplicados de 0.0.0.0 guardando NULL.
        - NO toca Mikrotik.
        """
        # A. Generar Credenciales Automáticas si faltan
        if not datos.user_pppoe:
            base = datos.nombre.lower().replace(" ", "")[:8]
            rand = random.randint(100, 999)
            datos.user_pppoe = f"{base}{rand}"
        
        if not datos.pass_pppoe:
            datos.pass_pppoe = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))

        # B. Manejo de IP para evitar error de base de datos
        ip_limpia = datos.ip_asignada.strip() if datos.ip_asignada else None
        if not ip_limpia or ip_limpia == "0.0.0.0":
            datos.ip_asignada = None 
        else:
            # Validar si la IP ya existe en otro cliente
            stmt = select(ClienteModel).where(ClienteModel.ip_asignada == ip_limpia)
            existing = await self.db.execute(stmt)
            ocupante = existing.scalar_one_or_none()
            if ocupante:
                raise ValueError(f"La IP {ip_limpia} ya la tiene: {ocupante.nombre}")

        # C. Forzar estado inicial
        datos.estado = "pendiente_instalacion"

        # D. Guardar en BD
        try:
            nuevo_cliente = await self.repo.create_cliente(datos)
            return await self._recargar_cliente(nuevo_cliente.id)
        except Exception as e:
            print(f"Error DB Registrar: {e}")
            raise Exception(f"Error al guardar en base de datos: {e}")

    # ==========================================
    # 2. ACTIVAR INSTALACIÓN (VERSION FINAL - VARIABLES SEGURAS)
    # ==========================================
    async def activar_instalacion(self, cliente_id: int, datos_finales: dict):
        """
        Activa el servicio en Mikrotik y dispara la notificación de bienvenida
        usando exclusivamente las variables autorizadas.
        """
        # A. Recuperar Cliente
        cliente = await self.db.get(ClienteModel, cliente_id)
        if not cliente: raise ValueError("Cliente no encontrado")

        # B. Actualizar Datos Técnicos desde el formulario
        if datos_finales.get('cedula'): cliente.cedula = datos_finales['cedula']
        if datos_finales.get('mac_address'): cliente.mac_address = datos_finales['mac_address']
        if datos_finales.get('router_id'): cliente.router_id = int(datos_finales['router_id'])
        if datos_finales.get('plan_id'): cliente.plan_id = int(datos_finales['plan_id'])
        
        # Gestión de IP
        ip_para_mikrotik = None
        if datos_finales.get('ip_asignada') and datos_finales.get('ip_asignada') != '0.0.0.0':
            cliente.ip_asignada = datos_finales['ip_asignada']
            ip_para_mikrotik = cliente.ip_asignada
        elif cliente.ip_asignada:
            ip_para_mikrotik = cliente.ip_asignada

        # Credenciales PPPoE
        if datos_finales.get('user_pppoe'): cliente.user_pppoe = datos_finales['user_pppoe']
        if datos_finales.get('pass_pppoe'): cliente.pass_pppoe = datos_finales['pass_pppoe']

        # E. Cargar Router y Plan para Mikrotik
        stmt_rel = select(ClienteModel).options(
            selectinload(ClienteModel.router), 
            selectinload(ClienteModel.plan),
            selectinload(ClienteModel.plantilla)
        ).where(ClienteModel.id == cliente_id)
        
        result_rel = await self.db.execute(stmt_rel)
        cliente_rel = result_rel.scalar_one()

        # F. ACTIVACIÓN EN MIKROTIK 🚀
        try:
            mk = MikroTikService(
                cliente_rel.router.ip_vpn, 
                cliente_rel.router.user_api, 
                cliente_rel.router.pass_api, 
                cliente_rel.router.port_api
            )
            
            comentario = f"{cliente.nombre} | SN:{cliente.cedula} | ID:{cliente.id}"

            mk.crear_actualizar_pppoe(
                user=cliente.user_pppoe,
                password=cliente.pass_pppoe,
                profile=cliente_rel.plan.nombre, 
                remote_address=ip_para_mikrotik,
                comment=comentario
            )
            
            # G. Guardar cambios en la base de datos
            cliente.estado = 'activo'
            cliente.fecha_instalacion = date.today()
            
            await self.db.commit()

            # --- 🚀 ENVÍO DE MENSAJE DE BIENVENIDA ---
            if cliente.telefono:
                try:
                    notificador = NotificationService(self.db)
                    
                    # Pasamos un diccionario vacío o con variables mínimas porque 
                    # tu NotificationService ya tiene la lógica para rellenar:
                    # {nombre}, {plan}, {precio}, {direccion}, {ip}, {dia_corte}, {empresa}, {fecha_actual}
                    
                    await notificador.notificar_evento("bienvenida", cliente.id)
                    print(f"✅ Notificación 'bienvenida' encolada para {cliente.nombre}")
                    
                except Exception as e_msg:
                    print(f"⚠️ Error al encolar bienvenida: {e_msg}")

            return await self._recargar_cliente(cliente.id)

        except Exception as e:
            await self.db.rollback()
            raise ValueError(f"Error en Mikrotik: {str(e)}")

    # ==========================================
    # 3. EDITAR CLIENTE (GENERAL)
    # ==========================================
    async def editar_cliente(self, cliente_id: int, datos: ClienteCreate):
        stmt = select(ClienteModel).where(ClienteModel.id == cliente_id)
        cliente_db = (await self.db.execute(stmt)).scalar_one_or_none()
        if not cliente_db: raise ValueError("Cliente no encontrado")

        for var, value in datos.dict(exclude_unset=True).items():
            setattr(cliente_db, var, value)
            
        await self.db.commit()
        
        if cliente_db.estado == 'activo':
            try:
                cliente_full = await self._recargar_cliente(cliente_id)
                await self._sincronizar_mikrotik(cliente_full)
            except Exception as e:
                print(f"⚠️ Error sincronizando: {e}")

        return await self._recargar_cliente(cliente_id)

    # ==========================================
    # 4. CAMBIAR ESTADO (CORTES)
    # ==========================================
    async def cambiar_estado(self, cliente_id: int, nuevo_estado: str):
        cliente = await self.db.get(ClienteModel, cliente_id)
        if not cliente: raise ValueError("Cliente no encontrado")

        cliente.estado = nuevo_estado
        await self.db.commit()
        await self.db.refresh(cliente, attribute_names=['router'])

        if cliente.router:
            try:
                mk = MikroTikService(cliente.router.ip_vpn, cliente.router.user_api, cliente.router.pass_api, cliente.router.port_api)
                if nuevo_estado in ["suspendido", "retirado", "cortado"]:
                    if cliente.user_pppoe: mk.desactivar_pppoe_user(cliente.user_pppoe)
                    else: mk.cortar_cliente(cliente.ip_asignada)
                else:
                    if cliente.user_pppoe: mk.activar_pppoe_user(cliente.user_pppoe)
                    else: mk.activar_cliente(cliente.ip_asignada)
            except: pass
        return f"Cliente {nuevo_estado}"

    # ==========================================
    # 5. ELIMINAR CLIENTE
    # ==========================================
    async def eliminar_cliente(self, cliente_id: int):
        cliente = await self.db.get(ClienteModel, cliente_id)
        if not cliente: raise ValueError("Cliente no encontrado")
        
        if cliente.router_id and cliente.user_pppoe:
            try:
                router = await self.db.get(RouterModel, cliente.router_id)
                mk = MikroTikService(router.ip_vpn, router.user_api, router.pass_api, router.port_api)
                mk.eliminar_pppoe_user(cliente.user_pppoe)
            except: pass

        await self.db.execute(delete(PagoModel).where(PagoModel.cliente_id == cliente_id))
        await self.db.execute(delete(FacturaModel).where(FacturaModel.cliente_id == cliente_id))
        await self.db.delete(cliente)
        await self.db.commit()
        return "Cliente eliminado"

    # ==========================================
    # 6. LISTADO UNIFICADO (DASHBOARD)
    # ==========================================
    async def get_listado_unificado(self):
        query = text("""
            SELECT c.id, c.nombre, c.cedula, c.telefono, c.direccion,
                   p.nombre as plan_nombre, p.precio as precio_plan,
                   c.ip_asignada, r.nombre as router_nombre, c.estado as estado_servicio,
                   nap.nombre as nap_nombre, c.puerto_nap,
                   COALESCE(count(f.id), 0) as facturas_pendientes_cant,
                   COALESCE(sum(f.saldo_pendiente), 0) as total_deuda,
                   c.saldo_a_favor
            FROM clientes c
            LEFT JOIN planes p ON c.plan_id = p.id
            LEFT JOIN routers r ON c.router_id = r.id
            LEFT JOIN cajas_nap nap ON c.caja_nap_id = nap.id 
            LEFT JOIN facturas f ON c.id = f.cliente_id AND f.estado = 'pendiente'
            WHERE c.estado != 'pendiente_instalacion'
            GROUP BY c.id ORDER BY c.id DESC
        """)
        result = await self.db.execute(query)
        rows = result.mappings().all()
        
        lista_final = []
        for row in rows:
            lista_final.append({
                "id": row.id, "nombre": row.nombre, "cedula": row.cedula or "",
                "telefono": row.telefono, "direccion": row.direccion,
                "servicio": {
                    "plan_nombre": row.plan_nombre or "Sin Plan",
                    "precio_plan": row.precio_plan or 0,
                    "ip_asignada": row.ip_asignada or "Pendiente",
                    "router_nombre": row.router_nombre or "Sin Router",
                    "estado_servicio": row.estado_servicio,
                    "nap_info": f"{row.nap_nombre} - P{row.puerto_nap}" if row.nap_nombre else "No Asignado"
                },
                "finanzas": {
                    "facturas_pendientes_cant": row.facturas_pendientes_cant,
                    "total_deuda": row.total_deuda,
                    "saldo_a_favor": row.saldo_a_favor,
                    "estado_financiero": "moroso" if row.total_deuda > 0 else "al_dia"
                }
            })
        return lista_final

    # ==========================================
    # HELPERS
    # ==========================================
    async def _recargar_cliente(self, cliente_id):
        stmt = select(ClienteModel).options(
            selectinload(ClienteModel.plan), selectinload(ClienteModel.router),
            selectinload(ClienteModel.plantilla), selectinload(ClienteModel.zona),
            selectinload(ClienteModel.caja_nap)
        ).where(ClienteModel.id == cliente_id)
        return (await self.db.execute(stmt)).scalar_one()

    async def _sincronizar_mikrotik(self, cliente):
        if not cliente.router or not cliente.plan or not cliente.user_pppoe: return
        mk = MikroTikService(cliente.router.ip_vpn, cliente.router.user_api, cliente.router.pass_api, cliente.router.port_api)
        mk.crear_actualizar_pppoe(
            user=cliente.user_pppoe, password=cliente.pass_pppoe,
            profile=cliente.plan.nombre, remote_address=cliente.ip_asignada,
            comment=f"{cliente.nombre} | ID:{cliente.id}"
        )