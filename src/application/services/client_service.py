from datetime import date, timedelta, datetime
import random
import string
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, text, and_
from sqlalchemy.orm import joinedload, selectinload 
from fastapi import BackgroundTasks, HTTPException
import re
from src.application.services.olt_service import OLTService
from src.application.services.billing_calendar_service import (
    BillingCalendarService,
)
from src.infrastructure.models import InventarioONUModel
from sqlalchemy import select, update

# Base de datos 
from src.infrastructure.database import SessionLocal as async_session 

# Modelos y Schemas
from src.infrastructure.models import (
    ClienteModel,
    PagoModel,
    RouterModel,
    FacturaModel,
    CajaNapModel,
    InventarioONUModel,
    ServicioModel,
    TipoFacturacion,
    CicloFacturacion,
)
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


    async def _obtener_o_crear_servicio(
        self,
        cliente: ClienteModel,
    ) -> ServicioModel:
        stmt = (
            select(ServicioModel)
            .where(
                ServicioModel.cliente_id == cliente.id,
                ServicioModel.estado != "cancelado",
            )
            .order_by(ServicioModel.id.desc())
        )

        resultado = await self.db.execute(stmt)
        servicio = resultado.scalars().first()
        if servicio:
            return servicio

        servicio = ServicioModel(
            cliente_id=cliente.id,
            plan_id=cliente.plan_id,
            plantilla_id=cliente.plantilla_id,
            tipo_facturacion=TipoFacturacion.prepago,
            ciclo_facturacion=CicloFacturacion.calendario,
            meses_gratis=1,
            estado="pendiente_instalacion",
        )
        self.db.add(servicio)
        await self.db.flush()
        return servicio

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

            # Creamos el contrato/servicio pendiente del abonado.
            await self._obtener_o_crear_servicio(nuevo_cliente)
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


        # FACTURACION_ISP_V2_IP_PPPOE_BACKEND_FIX
        if ip_para_mikrotik is not None:
            ip_para_mikrotik = str(ip_para_mikrotik).strip()

            if ip_para_mikrotik in {"", "0.0.0.0", "None", "null"}:
                ip_para_mikrotik = None
                cliente.ip_asignada = None

        if ip_para_mikrotik:
            cliente.ip_asignada = ip_para_mikrotik

        # Credenciales PPPoE
        if datos_finales.user_pppoe: cliente.user_pppoe = datos_finales.user_pppoe
        if datos_finales.pass_pppoe: cliente.pass_pppoe = datos_finales.pass_pppoe

        # E. Cargar Router y Plan para Mikrotik
        await self.db.flush()

        stmt_rel = select(ClienteModel).options(
            selectinload(ClienteModel.router), 
            selectinload(ClienteModel.plan),
            selectinload(ClienteModel.plantilla),
            selectinload(ClienteModel.onu_asignada)
        ).where(ClienteModel.id == cliente_id)
        
        result_rel = await self.db.execute(stmt_rel)
        cliente_rel = result_rel.scalar_one()


        # =====================================================
        # CONFIGURACIÓN COMERCIAL DEL SERVICIO
        # =====================================================
        servicio = await self._obtener_o_crear_servicio(cliente)

        fecha_instalacion = (
            datos_finales.fecha_instalacion
            or servicio.fecha_instalacion
            or date.today()
        )
        fecha_activacion = (
            datos_finales.fecha_activacion
            or servicio.fecha_activacion
            or fecha_instalacion
        )

        tipo_facturacion = TipoFacturacion(
            datos_finales.tipo_facturacion.value
        )
        ciclo_facturacion = CicloFacturacion(
            datos_finales.ciclo_facturacion.value
        )

        fechas_servicio = BillingCalendarService.calcular_fechas_servicio(
            fecha_instalacion=fecha_instalacion,
            fecha_activacion=fecha_activacion,
            meses_gratis=datos_finales.meses_gratis,
            ciclo_facturacion=ciclo_facturacion.value,
        )

        servicio.plan_id = cliente.plan_id
        servicio.plantilla_id = cliente.plantilla_id
        servicio.tipo_facturacion = tipo_facturacion
        servicio.ciclo_facturacion = ciclo_facturacion
        servicio.fecha_instalacion = fechas_servicio.fecha_instalacion
        servicio.fecha_activacion = fechas_servicio.fecha_activacion
        servicio.fecha_inicio_servicio = fechas_servicio.fecha_inicio_servicio
        servicio.fecha_fin_periodo_gratis = (
            fechas_servicio.fecha_fin_periodo_gratis
        )
        servicio.fecha_inicio_cobro = fechas_servicio.fecha_inicio_cobro
        servicio.proxima_facturacion = fechas_servicio.proxima_facturacion
        servicio.meses_gratis = datos_finales.meses_gratis
        servicio.estado = "activo"

        if cliente_rel.plantilla:
            servicio.dia_vencimiento = cliente_rel.plantilla.dia_pago
            servicio.dias_tolerancia = (
                cliente_rel.plantilla.dias_tolerancia or 0
            )
        else:
            servicio.dia_vencimiento = None
            servicio.dias_tolerancia = 0

        # Compatibilidad temporal con el motor anterior.
        cliente.proxima_factura = fechas_servicio.proxima_facturacion


        tipo_seguridad_router = None

        if cliente_rel.router:
            tipo_seguridad_router = getattr(
                cliente_rel.router.tipo_seguridad,
                "value",
                cliente_rel.router.tipo_seguridad,
            )

        if (
            str(tipo_seguridad_router).lower() == "pppoe"
            and not ip_para_mikrotik
        ):
            raise ValueError(
                "Debes seleccionar una IP libre antes de activar "
                "un cliente PPPoE. No se creará el usuario PPPoE "
                "sin remote-address."
            )

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
        from sqlalchemy import select, update
        from sqlalchemy.orm import selectinload
        from src.infrastructure.models import ClienteModel, InventarioONUModel
    
        # 1. Buscar el cliente en la base de datos
        stmt = select(ClienteModel).where(ClienteModel.id == cliente_id)
        cliente_db = (await self.db.execute(stmt)).scalar_one_or_none()
        
        if not cliente_db: 
            raise ValueError("Cliente no encontrado")
    
        # 2. Convertir esquema Pydantic a diccionario
        update_data = datos.model_dump(exclude_unset=True)
    
        # 3. LIMPIEZA DE LLAVES FORÁNEAS
        campos_fk = [
            "caja_nap_id", "puerto_nap", "router_id", "plan_id", 
            "tecnico_id", "plantilla_id", "zona_id", "red_id", "olt_id"
        ]
        for campo in campos_fk:
            if campo in update_data:
                if update_data[campo] in [0, "0", ""]:
                    update_data[campo] = None
    
        # 🔥 4. LÓGICA DE VINCULACIÓN DE HARDWARE BLINDADA 🔥
        sn_texto = update_data.get("identificador_onu") or update_data.get("mac_address")
    
        if sn_texto and sn_texto.strip() != "":
            sn_limpio = sn_texto.strip().upper()
            nuevo_onu_id = None
            
            stmt_inv = select(InventarioONUModel).where(InventarioONUModel.identificador == sn_limpio)
            onu_existente = (await self.db.execute(stmt_inv)).scalar_one_or_none()
    
            if onu_existente:
                nuevo_onu_id = onu_existente.id
                onu_existente.estado = "INSTALADO"
            else:
                nueva_onu = InventarioONUModel(
                    identificador=sn_limpio,
                    tecnologia="GPON", 
                    modelo="Auto-Generado",
                    estado="INSTALADO",
                    tecnico_id=update_data.get("tecnico_id") or cliente_db.tecnico_id
                )
                self.db.add(nueva_onu)
                await self.db.flush()
                nuevo_onu_id = nueva_onu.id

            # 🛡️ BLINDAJE: Si el cliente ya tenía una ONU y es DIFERENTE a la nueva, LA LIBERAMOS 🛡️
            if cliente_db.onu_id and cliente_db.onu_id != nuevo_onu_id:
                await self.db.execute(
                    update(InventarioONUModel)
                    .where(InventarioONUModel.id == cliente_db.onu_id)
                    .values(estado='DISPONIBLE', tecnico_id=None)
                )

            update_data["onu_id"] = nuevo_onu_id
        
        elif "onu_id" in update_data and update_data["onu_id"] in [0, None, ""]:
            # 🛡️ BLINDAJE: Si borran el campo de la ONU en el formulario, liberamos la actual
            if cliente_db.onu_id:
                await self.db.execute(
                    update(InventarioONUModel)
                    .where(InventarioONUModel.id == cliente_db.onu_id)
                    .values(estado='DISPONIBLE', tecnico_id=None)
                )
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
        if cliente_db.estado == 'activo':
            try:
                cliente_full = await self._recargar_cliente(cliente_id)
                if cliente_full.router and cliente_full.plan:
                    await self._sincronizar_mikrotik(cliente_full)
            except Exception as e:
                print(f"⚠️ Error MikroTik (Perfil/Conexión): {e}")
    
        # 7. RESPUESTA FINAL RECARGADA
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
        
        # 👇 NOTIFICACIÓN DE PROMESA SINCRONIZADA 👇
        if cliente.telefono:
            try:
                notificador = NotificationService(self.db)
                # Formateamos la fecha elegida
                fecha_promesa_str = fecha_promesa.strftime("%d/%m/%Y")
                
                # Pasamos exactamente las variables que lee la plantilla 8
                await notificador.notificar(
                    tipo_evento="promesa_pago", 
                    cliente_id=cliente.id, 
                    variables_extra={
                        "fecha_limite_promesa": fecha_promesa_str,              # 🔥 Ajustado para la plantilla
                        "monto_promesa": f"${factura.saldo_pendiente:.2f}"     # 🔥 Añadido para la plantilla
                    }
                )
            except Exception as e:
                print(f"⚠️ Error notificación promesa: {e}")
        
        msg = f"Promesa exitosa hasta el {fecha_promesa}."
        if reactivado:
            msg += " 📡 Servicio reactivado en MikroTik."
            
        return msg
    

# ==========================================
    # 8. REACTIVAR SERVICIO CANCELADO
    # ==========================================
    async def reactivar_servicio(self, cliente_id: int):
        """
        Toma un cliente en estado 'cancelado', lo devuelve a 'activo' 
        y vuelve a crear su usuario PPPoE en el MikroTik.
        """
        # 1. Buscamos al cliente con sus relaciones necesarias
        stmt = select(ClienteModel).options(
            selectinload(ClienteModel.router), 
            selectinload(ClienteModel.plan)
        ).where(ClienteModel.id == cliente_id)
        
        cliente = (await self.db.execute(stmt)).scalar_one_or_none()
        
        if not cliente:
            raise ValueError("Cliente no encontrado")
            
        if cliente.estado != 'cancelado':
            raise ValueError("El cliente no está cancelado, no se puede reactivar.")

        # 2. Conectamos al MikroTik y creamos el usuario PPPoE de nuevo
        if cliente.router and cliente.plan and cliente.user_pppoe and cliente.pass_pppoe:
            try:
                mk = MikroTikService(
                    cliente.router.ip_vpn, 
                    cliente.router.user_api, 
                    cliente.router.pass_api, 
                    cliente.router.port_api
                )
                
                cedula_str = cliente.cedula if cliente.cedula else "S/A"
                comentario_estandar = f"{cliente.nombre} | ID:{cedula_str}"
                
                # Usamos el mismo método que en la instalación para asegurar que todo quede idéntico
                mk.crear_actualizar_pppoe(
                    user=cliente.user_pppoe, 
                    password=cliente.pass_pppoe, 
                    profile=cliente.plan.nombre, 
                    remote_address=cliente.ip_asignada,
                    comment=comentario_estandar
                )
            except Exception as e:
                raise ValueError(f"Error al conectar con MikroTik durante reactivación: {e}")
        else:
            print(f"⚠️ Aviso: Cliente {cliente_id} reactivado en BD, pero le falta Router, Plan o PPPoE para MikroTik.")

        # 3. Lo marcamos como activo en la base de datos
        cliente.estado = 'activo'
        await self.db.commit()
        
        return "Servicio reactivado y conectado al MikroTik exitosamente."


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

        cliente.estado = 'cancelado'
        
        if cliente.onu_id:
            onu = await self.db.get(InventarioONUModel, cliente.onu_id)
            if onu:
                onu.estado = 'POR_RECOGER' 
                await self.db.commit()
                return f"Servicio cancelado. La ONU {onu.identificador} está pendiente de recolección."

        await self.db.commit()
        return "Servicio cancelado. (Este cliente no tenía un equipo de bodega asignado)."

    # 🔥 CORRECCIÓN DEL NULL: Ahora recibe inventario_id en lugar de cliente_id 🔥
    async def confirmar_retiro_tecnico(self, inventario_id: int):
        onu = await self.db.get(InventarioONUModel, inventario_id)
        if not onu:
            raise ValueError("Este equipo no existe en el inventario.")

        if onu.estado != 'POR_RECOGER':
            raise ValueError("Este equipo no está marcado para recolección.")

        serial_recuperado = onu.identificador

        # 1. Regresamos la ONU a Bodega
        onu.estado = 'DISPONIBLE'
        onu.tecnico_id = None 
        
        # 2. Buscamos si algún cliente tenía esta ONU asignada y lo liberamos
        from sqlalchemy import select
        stmt = select(ClienteModel).where(ClienteModel.onu_id == inventario_id)
        cliente = (await self.db.execute(stmt)).scalar_one_or_none()
        
        if cliente:
            cliente.onu_id = None             
            cliente.caja_nap_id = None        
            cliente.puerto_nap = None         
            cliente.ip_asignada = None        
        
        await self.db.commit()
        return f"¡Éxito! ONU {serial_recuperado} ingresada a stock."

    async def asignar_tecnico_retiro(self, inventario_id: int, tecnico_id: int):
        # Buscamos la ONU directo en el inventario
        onu = await self.db.get(InventarioONUModel, inventario_id)
        if not onu or onu.estado != 'POR_RECOGER':
            raise ValueError("Esta ONU no tiene un retiro pendiente o no existe.")

        onu.tecnico_id = tecnico_id 
        await self.db.commit()
        
        return f"Retiro asignado al técnico ID: {tecnico_id}"

    # 🔥 NUEVA FUNCIÓN: SWAP DE ONU POR FALLA 🔥
    async def procesar_cambio_onu(self, cliente_id: int, nuevo_inventario_id: int, estado_vieja_onu: str):
        # 1. Validaciones iniciales
        cliente = await self.db.get(ClienteModel, cliente_id)
        if not cliente:
            raise ValueError("Cliente no encontrado")

        onu_nueva = await self.db.get(InventarioONUModel, nuevo_inventario_id)
        if not onu_nueva or onu_nueva.estado != 'DISPONIBLE':
            raise ValueError("La nueva ONU no existe o no está DISPONIBLE.")

        estado_limpio = estado_vieja_onu.upper().strip()
        if estado_limpio in ["DAÑADA", "DANADA", "DAÑADO", "DANADO", "CON_FALLA", "FALLA"]:
            estado_limpio = "CON_FALLA"

        # ==========================================
        # PASO 1: LIBERAR LA ONU VIEJA (Fuerza Bruta)
        # ==========================================
        if cliente.onu_id:
            await self.db.execute(
                update(InventarioONUModel)
                .where(InventarioONUModel.id == cliente.onu_id)
                .values(estado=estado_limpio, tecnico_id=None)
            )

        # ==========================================
        # PASO 2: ASIGNAR LA NUEVA ONU AL CLIENTE
        # ==========================================
        await self.db.execute(
            update(InventarioONUModel)
            .where(InventarioONUModel.id == nuevo_inventario_id)
            .values(estado='INSTALADO', tecnico_id=cliente.tecnico_id)
        )

        # ==========================================
        # PASO 3: ACTUALIZAR EL PERFIL DEL CLIENTE
        # ==========================================
        await self.db.execute(
            update(ClienteModel)
            .where(ClienteModel.id == cliente_id)
            .values(
                onu_id=nuevo_inventario_id,
                mac_address=onu_nueva.identificador
            )
        )

        # Confirmamos los cambios directamente en el disco duro de la BD
        await self.db.commit()
        
        return f"Cambio exitoso. Nueva ONU asignada: {onu_nueva.identificador}."
