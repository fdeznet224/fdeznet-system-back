from datetime import date, timedelta, datetime
import random
import string
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, text, and_
from sqlalchemy.orm import joinedload, selectinload 
from fastapi import BackgroundTasks, HTTPException
import re
from src.application.services.olt_service import OLTService
from src.infrastructure.models import InventarioONUModel

# Base de datos 
from src.infrastructure.database import SessionLocal as async_session 

# Modelos y Schemas
from src.infrastructure.models import ClienteModel, PagoModel, RouterModel, FacturaModel, CajaNapModel 
from src.domain.schemas import ClienteCreate, InstalacionRequest
from src.infrastructure.repositories import ClienteRepository

# Servicios Externos
from src.infrastructure.mikrotik_service import MikroTikService

# 👇 Importamos el MEGA NOTIFICADOR 👇
#from src.application.helpers.notification_manager import enviar_notificacion_automatica
from src.application.services.notification_service import NotificationService

class ClientService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = ClienteRepository(db)
        self.olt_service = OLTService(db)

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
        Crea la orden en BD, cambia el estado del equipo en Inventario a 'INSTALADO'
        y genera un ID Hexadecimal Aleatorio.
        """
        import random
        import string
        from sqlalchemy.exc import IntegrityError 

        if datos.nombre:
            datos.nombre = datos.nombre.strip()

        # A. Generar Credenciales Automáticas
        if not datos.user_pppoe and datos.nombre:
            base = datos.nombre.lower().replace(" ", "")[:8]
            rand = random.randint(100, 999)
            datos.user_pppoe = f"{base}{rand}"
        
        if not datos.pass_pppoe:
            datos.pass_pppoe = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))

        # B. Manejo de IP
        ip_limpia = datos.ip_asignada.strip() if datos.ip_asignada else None
        if not ip_limpia or ip_limpia == "0.0.0.0":
            datos.ip_asignada = None 
        else:
            stmt = select(ClienteModel).where(ClienteModel.ip_asignada == ip_limpia)
            existing = await self.db.execute(stmt)
            ocupante = existing.scalar_one_or_none()
            if ocupante:
                raise ValueError(f"La IP {ip_limpia} ya la tiene: {ocupante.nombre}")

        # D. Preparamos el objeto para la Base de Datos
        datos_dict = datos.dict(exclude={"cedula"}) if hasattr(datos, 'dict') else datos.model_dump(exclude={"cedula"})
        
        # Convertir 0 o "" a None en llaves foráneas
        for fk in ["olt_id", "onu_id", "router_id", "plan_id", "zona_id", "plantilla_id", "caja_nap_id", "tecnico_id", "red_id"]:
            if datos_dict.get(fk) in [0, "0", ""]:
                datos_dict[fk] = None

        datos_dict['estado'] = "pendiente_instalacion"

        # 🔥 AQUÍ ESTÁ EL ESCUDO: Quitamos la basura virtual antes de guardar 🔥
        datos_dict.pop("identificador_onu", None)
        datos_dict.pop("mac_address", None)

        # 🚀 Ahora sí, la base de datos lo aceptará sin quejarse
        nuevo_cliente = ClienteModel(**datos_dict)
        self.db.add(nuevo_cliente)

        try:
            await self.db.flush() 
            
            # 🔥 MAGIA DEL INVENTARIO: Marcamos la ONU como INSTALADA
            if nuevo_cliente.onu_id:
                onu = await self.db.get(InventarioONUModel, nuevo_cliente.onu_id)
                if onu:
                    if onu.estado != "DISPONIBLE":
                        raise ValueError(f"El equipo {onu.identificador} no está disponible (Estado actual: {onu.estado}).")
                    
                    onu.estado = 'INSTALADO'
                    if nuevo_cliente.tecnico_id:
                        onu.tecnico_id = nuevo_cliente.tecnico_id
            
            # E. LÓGICA: HEXADECIMAL ALEATORIO
            caracteres_hex = "0123456789ABCDEF"
            while True:
                codigo_hex = ''.join(random.choices(caracteres_hex, k=4))
                stmt_check = select(ClienteModel).where(ClienteModel.cedula == codigo_hex)
                existe = await self.db.execute(stmt_check)
                if not existe.scalar_one_or_none():
                    break
            
            nuevo_cliente.cedula = codigo_hex

            # F. Guardamos permanentemente
            await self.db.commit()
            await self.db.refresh(nuevo_cliente)
            
            return await self._recargar_cliente(nuevo_cliente.id)
            
        except IntegrityError as e:
            await self.db.rollback()
            print(f"Error de Integridad DB: {e}")
            raise ValueError("No se pudo registrar: Hay un dato duplicado en el sistema (IP ocupada).")
        except Exception as e:
            await self.db.rollback()
            print(f"Error DB Registrar: {e}")
            raise Exception(f"Error al generar cliente: {e}")

    # ==========================================
    # 2. ACTIVAR INSTALACIÓN (VERSION FINAL - VARIABLES SEGURAS)
    # ==========================================
    async def activar_instalacion(self, cliente_id: int, datos_finales: InstalacionRequest):
        """
        Activa el servicio en Mikrotik. Si el técnico cambia la ONU, actualiza el inventario.
        """
        # A. Recuperar Cliente
        cliente = await self.db.get(ClienteModel, cliente_id)
        if not cliente: raise ValueError("Cliente no encontrado")

        if datos_finales.cedula is not None:
            cliente.cedula = datos_finales.cedula
            
        if hasattr(datos_finales, 'olt_id') and datos_finales.olt_id is not None:
            cliente.olt_id = datos_finales.olt_id
            
        # 👇👇👇 NUEVA LÓGICA PARA ASIGNAR ONU DESDE INVENTARIO AL ACTIVAR 👇👇👇
        if hasattr(datos_finales, 'onu_id') and datos_finales.onu_id is not None:
            # Si el técnico asigna una ONU nueva al momento de activar
            if cliente.onu_id != datos_finales.onu_id:
                
                # 1. Liberamos la ONU vieja si tenía una (La regresamos a BODEGA)
                if cliente.onu_id:
                    onu_vieja = await self.db.get(InventarioONUModel, cliente.onu_id)
                    if onu_vieja: onu_vieja.estado = 'DISPONIBLE'
                
                # 2. Asignamos la nueva y la marcamos INSTALADA
                onu_nueva = await self.db.get(InventarioONUModel, datos_finales.onu_id)
                if not onu_nueva: raise ValueError("El equipo seleccionado no existe en Bodega.")
                if onu_nueva.estado != "DISPONIBLE": raise ValueError("Ese equipo ya no está disponible.")
                
                onu_nueva.estado = 'INSTALADO'
                if cliente.tecnico_id: onu_nueva.tecnico_id = cliente.tecnico_id
                
                cliente.onu_id = datos_finales.onu_id
        # 👆👆👆 FIN NUEVA LÓGICA 👆👆👆

        # PROTECCIÓN CLAVE: Solo actualizar si el técnico lo envió
        if datos_finales.router_id is not None: cliente.router_id = datos_finales.router_id
        if datos_finales.plan_id is not None: cliente.plan_id = datos_finales.plan_id
        
        # INFRAESTRUCTURA FIBRA Y GPS
        if datos_finales.caja_nap_id is not None: cliente.caja_nap_id = datos_finales.caja_nap_id
        if datos_finales.puerto_nap is not None: cliente.puerto_nap = datos_finales.puerto_nap
        if datos_finales.latitud is not None: cliente.latitud = datos_finales.latitud
        if datos_finales.longitud is not None: cliente.longitud = datos_finales.longitud
        
        # Gestión de IP
        ip_para_mikrotik = None
        if datos_finales.ip_asignada and datos_finales.ip_asignada != '0.0.0.0':
            cliente.ip_asignada = datos_finales.ip_asignada
            ip_para_mikrotik = cliente.ip_asignada
        elif cliente.ip_asignada:
            ip_para_mikrotik = cliente.ip_asignada

        # Credenciales PPPoE
        if datos_finales.user_pppoe: cliente.user_pppoe = datos_finales.user_pppoe
        if datos_finales.pass_pppoe: cliente.pass_pppoe = datos_finales.pass_pppoe

        # E. Cargar Router y Plan para Mikrotik
        stmt_rel = select(ClienteModel).options(
            selectinload(ClienteModel.router), 
            selectinload(ClienteModel.plan),
            selectinload(ClienteModel.plantilla),
            selectinload(ClienteModel.onu_asignada)
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
            
            cedula_str = cliente.cedula if cliente.cedula else "S/A"
            comentario_estandar = f"{cliente.nombre} | ID:{cedula_str}"

            mk.crear_actualizar_pppoe(
                user=cliente.user_pppoe,
                password=cliente.pass_pppoe,
                profile=cliente_rel.plan.nombre, 
                remote_address=ip_para_mikrotik,
                comment=comentario_estandar
            )
            
            # G. Guardar cambios en la base de datos
            cliente.estado = 'activo'
            # Ya no necesitas `fecha_instalacion` si no está en tu modelo actual, si está déjalo.
            
            await self.db.commit()

            # --- 🚀 ENVÍO DE MENSAJE DE BIENVENIDA ---
            if cliente.telefono:
                try:
                    notificador = NotificationService(self.db)
                    await notificador.notificar("bienvenida", cliente.id)
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
        """
        Actualiza un cliente y gestiona la vinculación de hardware (ONU) 
        ya sea por ID numérico o por Identificador (SN/MAC).
        """
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload
        from src.infrastructure.models import ClienteModel, InventarioONUModel
    
        # 1. Buscar el cliente en la base de datos
        stmt = select(ClienteModel).where(ClienteModel.id == cliente_id)
        cliente_db = (await self.db.execute(stmt)).scalar_one_or_none()
        
        if not cliente_db: 
            raise ValueError("Cliente no encontrado")
    
        # 2. Convertir esquema Pydantic a diccionario (solo datos enviados)
        update_data = datos.model_dump(exclude_unset=True)
    
        # 3. LIMPIEZA DE LLAVES FORÁNEAS (Evita errores de integridad)
        campos_fk = [
            "caja_nap_id", "puerto_nap", "router_id", "plan_id", 
            "tecnico_id", "plantilla_id", "zona_id", "red_id", "olt_id"
        ]
        for campo in campos_fk:
            if campo in update_data:
                # Si el valor es 0, "0" o vacío, lo mandamos como NULL
                if update_data[campo] in [0, "0", ""]:
                    update_data[campo] = None
    
        # 🔥 4. LÓGICA DE VINCULACIÓN DE HARDWARE (ONU) 🔥
        # Buscamos si el usuario envió el SN/MAC en 'identificador_onu' o 'mac_address'
        sn_texto = update_data.get("identificador_onu") or update_data.get("mac_address")
    
        if sn_texto and sn_texto.strip() != "":
            sn_limpio = sn_texto.strip().upper()
            
            # A. Buscamos si el SN ya existe en el inventario
            stmt_inv = select(InventarioONUModel).where(InventarioONUModel.identificador == sn_limpio)
            res_inv = await self.db.execute(stmt_inv)
            onu_existente = res_inv.scalar_one_or_none()
    
            if onu_existente:
                # Si existe, tomamos su ID y la marcamos como instalada
                update_data["onu_id"] = onu_existente.id
                onu_existente.estado = "INSTALADO"
            else:
                # B. Si no existe (caso Excel), la creamos automáticamente
                nueva_onu = InventarioONUModel(
                    identificador=sn_limpio,
                    tecnologia="GPON", 
                    modelo="Auto-Generado",
                    estado="INSTALADO",
                    tecnico_id=update_data.get("tecnico_id") or cliente_db.tecnico_id
                )
                self.db.add(nueva_onu)
                await self.db.flush() # Para obtener el ID autogenerado
                update_data["onu_id"] = nueva_onu.id
        
        elif "onu_id" in update_data and update_data["onu_id"] in [0, None, ""]:
            # C. Si el campo se envió vacío, desvinculamos la ONU anterior
            if cliente_db.onu_id:
                onu_v = await self.db.get(InventarioONUModel, cliente_db.onu_id)
                if onu_v: 
                    onu_v.estado = "DISPONIBLE"
            update_data["onu_id"] = None
    
        # 5. APLICAR CAMBIOS AL MODELO
        for var, value in update_data.items():
            setattr(cliente_db, var, value)
                
        try:
            await self.db.commit()
        except Exception as e:
            await self.db.rollback()
            raise ValueError(f"Error al persistir cambios: {str(e)}")
        
        # 6. SINCRONIZACIÓN CON MIKROTIK
        # Solo si el cliente está activo y tiene plan/router
        if cliente_db.estado == 'activo':
            try:
                # Recargamos con todas las relaciones necesarias para el script de MK
                cliente_full = await self._recargar_cliente(cliente_id)
                if cliente_full.router and cliente_full.plan:
                    await self._sincronizar_mikrotik(cliente_full)
            except Exception as e:
                # Logeamos el error pero no bloqueamos la respuesta al usuario
                print(f"⚠️ Error MikroTik (Perfil/Conexión): {e}")
    
        # 7. RESPUESTA FINAL RECARGADA (Crucial para ver los datos en el Front)
        return await self._recargar_cliente(cliente_id)

    # ==========================================
    # 4. CAMBIAR ESTADO (CORTES)
    # ==========================================
    async def cambiar_estado(self, cliente_id: int, nuevo_estado: str):
        cliente = await self.db.get(ClienteModel, cliente_id)
        if not cliente: 
            raise ValueError("Cliente no encontrado")

        # 1. Actualizar estado en Base de Datos
        estado_limpio = nuevo_estado.lower().strip()
        cliente.estado = estado_limpio
        
        await self.db.commit()
        await self.db.refresh(cliente, attribute_names=['router'])

        if cliente.router:
            # Instanciar el notificador unificado
            notificador = NotificationService(self.db)
            
            try:
                mk = MikroTikService(
                    cliente.router.ip_vpn, 
                    cliente.router.user_api, 
                    cliente.router.pass_api, 
                    cliente.router.port_api
                )

                if estado_limpio in ["suspendido", "retirado", "cortado"]:
                    # --- LÓGICA MIKROTIK ---
                    try:
                        if cliente.user_pppoe:
                            # ✅ USAMOS EL NOMBRE REAL DE TU FUNCIÓN: activar_desactivar_pppoe
                            # disabled=True para suspender
                            mk.activar_desactivar_pppoe(cliente.user_pppoe, disabled=True)
                        else:
                            # Fallback para IPs estáticas si usas Address-List
                            mk.gestionar_corte_cliente(cliente.ip_asignada, suspender=True)
                    except Exception as e_mk:
                        print(f"⚠️ MikroTik no pudo procesar la suspensión: {e_mk}")

                    # --- 🚀 WHATSAPP DE CORTE ---
                    if cliente.telefono:
                        await notificador.notificar("corte_servicio", cliente.id)
                        print(f"✅ WhatsApp de SUSPENSIÓN encolado para {cliente.nombre}")
                
                elif estado_limpio == "activo":
                    # --- LÓGICA MIKROTIK ---
                    try:
                        if cliente.user_pppoe:
                            # ✅ disabled=False para activar
                            mk.activar_desactivar_pppoe(cliente.user_pppoe, disabled=False)
                        else:
                            mk.gestionar_corte_cliente(cliente.ip_asignada, suspender=False)
                    except Exception as e_mk:
                        print(f"⚠️ MikroTik no pudo procesar la activación: {e_mk}")

                    # --- 🚀 WHATSAPP DE RECONEXIÓN ---
                    if cliente.telefono:
                        await notificador.notificar("reconexion", cliente.id)
                        print(f"✅ WhatsApp de RECONEXIÓN encolado para {cliente.nombre}")
                            
            except Exception as e:
                # Captura errores de conexión al Router sin detener el sistema
                print(f"❌ Error en el proceso de cambio de estado: {e}")
                
        return f"Cliente {estado_limpio}"

    # ==========================================
    # 5. ELIMINAR CLIENTE
    # ==========================================
    async def eliminar_cliente(self, cliente_id: int):
        cliente = await self.db.get(ClienteModel, cliente_id)
        if not cliente: 
            raise ValueError("Cliente no encontrado")
        
        # 🔥 PASO 1: LIBERAR LA ONU EN EL INVENTARIO ANTES DE BORRAR AL CLIENTE
        if cliente.onu_id:
            onu = await self.db.get(InventarioONUModel, cliente.onu_id)
            if onu:
                onu.estado = 'DISPONIBLE'
                onu.tecnico_id = None
                print(f"📦 ONU {onu.identificador} regresada a DISPONIBLE.")

        # PASO 2: ELIMINAR DEL MIKROTIK
        if cliente.router_id and cliente.user_pppoe:
            try:
                router = await self.db.get(RouterModel, cliente.router_id)
                mk = MikroTikService(router.ip_vpn, router.user_api, router.pass_api, router.port_api)
                mk.eliminar_pppoe_user(cliente.user_pppoe)
            except: 
                pass

        # PASO 3: LIMPIEZA DE BASE DE DATOS (Cascada)
        await self.db.execute(delete(PagoModel).where(PagoModel.cliente_id == cliente_id))
        await self.db.execute(delete(FacturaModel).where(FacturaModel.cliente_id == cliente_id))
        
        # PASO 4: ELIMINAR AL CLIENTE
        await self.db.delete(cliente)
        
        # Guardamos todos los cambios (Liberación de ONU y Borrados) de un solo golpe
        await self.db.commit()
        
        return "Cliente eliminado y equipo liberado correctamente"

    # ==========================================
    # 5. PROMESA D EPAGO
    # ==========================================
    async def registrar_promesa_pago(self, cliente_id: int, fecha_promesa: date):
        """
        Registra una promesa de pago y reactiva el servicio si es necesario.
        """
        # 1. Buscar factura pendiente más antigua
        stmt_f = select(FacturaModel).where(
            FacturaModel.cliente_id == cliente_id,
            FacturaModel.estado == 'pendiente'
        ).order_by(FacturaModel.fecha_vencimiento.asc())
        
        res_f = await self.db.execute(stmt_f)
        factura = res_f.scalars().first()
        
        if not factura:
            raise ValueError("El cliente no tiene facturas pendientes para aplicar promesa.")

        # 2. Aplicar promesa a la factura
        factura.es_promesa_activa = True
        factura.fecha_promesa_pago = fecha_promesa

        # 3. Reactivar cliente si está suspendido
        cliente = await self.db.get(ClienteModel, cliente_id)
        reactivado = False
        
        if cliente.estado == 'suspendido':
            cliente.estado = 'activo'
            # Usamos la lógica que ya tienes en BillingService
            from src.application.services.billing_service import BillingService
            b_service = BillingService(self.db)
            reactivado = await b_service._reactivar_en_mikrotik(cliente)

        await self.db.commit()
        
        # 👇 NOTIFICACIÓN DE PROMESA 👇
        if cliente.telefono:
            try:
                notificador = NotificationService(self.db)
                # Usamos el método .notificar y pasamos las variables extra
                await notificador.notificar(
                    tipo_evento="promesa_pago", 
                    cliente_id=cliente.id, 
                    variables_extra={"fecha_limite": fecha_promesa.strftime("%d/%m/%Y")}
                )
            except Exception as e:
                print(f"⚠️ Error notificación promesa: {e}")
        
        msg = f"Promesa exitosa hasta el {fecha_promesa}."
        if reactivado:
            msg += " 📡 Servicio reactivado en MikroTik."
            
        return msg

    # ==========================================
    # 6. LISTADO UNIFICADO (DASHBOARD)
    # ==========================================
    async def get_listado_unificado(self):
        query = text("""
            SELECT c.id, c.nombre, c.cedula, c.telefono, c.direccion,
                   c.latitud, c.longitud,
                   z.nombre as zona_nombre,
                   p.nombre as plan_nombre, p.precio as precio_plan,
                   c.ip_asignada, r.nombre as router_nombre, c.estado as estado_servicio,
                   nap.nombre as nap_nombre, c.puerto_nap,
                   COALESCE(count(f.id), 0) as facturas_pendientes_cant,
                   COALESCE(sum(f.saldo_pendiente), 0) as total_deuda,
                   c.saldo_a_favor,
                   inv.identificador as onu_identificador, -- 👇 Agregamos el identificador de la ONU
                   inv.modelo as onu_modelo -- 👇 Agregamos el modelo de la ONU
            FROM clientes c
            LEFT JOIN planes p ON c.plan_id = p.id
            LEFT JOIN routers r ON c.router_id = r.id
            LEFT JOIN cajas_nap nap ON c.caja_nap_id = nap.id 
            LEFT JOIN zonas z ON c.zona_id = z.id
            LEFT JOIN inventario_onus inv ON c.onu_id = inv.id -- 👇 Cruzamos con el inventario
            LEFT JOIN facturas f ON c.id = f.cliente_id AND f.estado = 'pendiente'
            WHERE c.estado != 'pendiente_instalacion'
            GROUP BY c.id ORDER BY c.id DESC
        """)
        result = await self.db.execute(query)
        rows = result.mappings().all()
        
        lista_final = []
        for row in rows:
            # Formateamos cómo queremos que se vea el hardware
            hardware_info = "Sin registrar"
            if row.onu_identificador:
                hardware_info = f"{row.onu_identificador} ({row.onu_modelo or 'Genérico'})"

            lista_final.append({
                "id": row.id, "nombre": row.nombre, "cedula": row.cedula or "",
                "telefono": row.telefono, "direccion": row.direccion,
                "latitud": row.latitud,   
                "longitud": row.longitud, 
                "zona": row.zona_nombre or "Sin Zona",
                "identificador_onu": hardware_info, # 👇 Esto lo leerá el frontend (la tarjeta)
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
            selectinload(ClienteModel.plan), 
            selectinload(ClienteModel.router),
            selectinload(ClienteModel.plantilla), 
            selectinload(ClienteModel.zona),
            selectinload(ClienteModel.caja_nap),
            selectinload(ClienteModel.tecnico),
            selectinload(ClienteModel.olt),
            selectinload(ClienteModel.onu_asignada)
        ).where(ClienteModel.id == cliente_id)
        
        return (await self.db.execute(stmt)).scalar_one()

    async def _sincronizar_mikrotik(self, cliente):
        if not cliente.router or not cliente.plan or not cliente.user_pppoe: return
        mk = MikroTikService(cliente.router.ip_vpn, cliente.router.user_api, cliente.router.pass_api, cliente.router.port_api)
        
        cedula_str = cliente.cedula if cliente.cedula else "S/A"
        mk.crear_actualizar_pppoe(
            user=cliente.user_pppoe, password=cliente.pass_pppoe,
            profile=cliente.plan.nombre, remote_address=cliente.ip_asignada,
            comment=f"{cliente.nombre} | ID:{cedula_str}"
        )



    # ==========================================
    # GESTIÓN DE BAJAS E INVENTARIO DE ONUS
    # ==========================================

    async def procesar_baja_servicio(self, cliente_id: int):
        """
        CLIC 1 (Admin/Cajera): Corta el internet y manda la ONU física a POR_RECOGER en el inventario.
        """
        stmt = select(ClienteModel).options(selectinload(ClienteModel.router)).where(ClienteModel.id == cliente_id)
        cliente = (await self.db.execute(stmt)).scalar_one_or_none()
        
        if not cliente:
            raise ValueError("Cliente no encontrado")

        if cliente.router and cliente.user_pppoe:
            try:
                mk = MikroTikService(
                    cliente.router.ip_vpn, 
                    cliente.router.user_api, 
                    cliente.router.pass_api, 
                    cliente.router.port_api
                )
                mk.eliminar_pppoe_user(cliente.user_pppoe)
            except Exception as e:
                print(f"⚠️ Aviso: No se pudo conectar a MikroTik al dar de baja: {e}")

        # Actualizar BD del Cliente
        cliente.estado = 'cancelado'
        
        # 👇 MAGIA DEL INVENTARIO: Cambiamos el estado de la ONU física 👇
        if cliente.onu_id:
            onu = await self.db.get(InventarioONUModel, cliente.onu_id)
            if onu:
                onu.estado = 'POR_RECOGER' 
                await self.db.commit()
                return f"Servicio cancelado. La ONU {onu.identificador} está pendiente de recolección."

        await self.db.commit()
        return "Servicio cancelado. (Este cliente no tenía un equipo de bodega asignado)."

    # BORRA EL SEGUNDO 'confirmar_retiro_tecnico' QUE TIENES DUPLICADO, DEJA SOLO ESTE:
    async def confirmar_retiro_tecnico(self, cliente_id: int):
        """
        CLIC 2 (Técnico): Confirma que ya tiene la ONU y la regresa a bodega.
        """
        cliente = await self.db.get(ClienteModel, cliente_id)
        if not cliente:
            raise ValueError("Cliente no encontrado")

        # 👇 MAGIA DEL INVENTARIO 👇
        if not cliente.onu_id:
            raise ValueError("Este cliente no tiene equipo asignado en el inventario.")

        onu = await self.db.get(InventarioONUModel, cliente.onu_id)
        if not onu or onu.estado != 'POR_RECOGER':
            raise ValueError("Este equipo no está marcado para recolección.")

        serial_recuperado = onu.identificador

        # 1. Regresamos la ONU a Bodega
        onu.estado = 'DISPONIBLE'
        onu.tecnico_id = None # Le quitamos la responsabilidad al técnico
        
        # 2. 🔥 LIBERACIÓN ABSOLUTA DEL CLIENTE 🔥
        cliente.onu_id = None             # Desvinculamos el equipo
        cliente.caja_nap_id = None        # El poste queda libre
        cliente.puerto_nap = None         # El puerto queda libre
        cliente.ip_asignada = None        # La IP queda libre
        
        await self.db.commit()
        return f"¡Éxito! ONU {serial_recuperado} ingresada a stock y puerto liberado."

    async def asignar_tecnico_retiro(self, cliente_id: int, tecnico_id: int):
        """
        Asigna un técnico específico para ir a recoger el equipo.
        """
        cliente = await self.db.get(ClienteModel, cliente_id)
        if not cliente or not cliente.onu_id:
            raise ValueError("Cliente no encontrado o no tiene equipo.")
        
        onu = await self.db.get(InventarioONUModel, cliente.onu_id)
        if not onu or onu.estado != 'POR_RECOGER':
            raise ValueError("Esta ONU no tiene un retiro pendiente.")

        onu.tecnico_id = tecnico_id # Asignamos al técnico elegido a la ONU física
        await self.db.commit()
        
        return f"Retiro asignado al técnico ID: {tecnico_id}"