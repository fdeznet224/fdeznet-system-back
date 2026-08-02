from datetime import date, timedelta, datetime
from decimal import Decimal
import random
import string
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, text, and_, func
from sqlalchemy.orm import joinedload, selectinload 
from fastapi import BackgroundTasks, HTTPException
import re
from src.application.services.olt_service import OLTService
from src.application.services.billing_calendar_service import (
    BillingCalendarService,
)
from sqlalchemy import select, update

# Base de datos 
from src.infrastructure.database import SessionLocal as async_session 

# Modelos y Schemas
from src.infrastructure.models import (
    BajaServicioModel,
    ClienteModel,
    PagoModel,
    RouterModel,
    FacturaModel,
    CajaNapModel,
    InventarioONUModel,
    OLTModel,
    PlanModel,
    ServicioModel,
    TipoFacturacion,
    CicloFacturacion,
    HistorialEstadoOrdenModel,
    OrdenServicioModel,
    PuertoNapModel,
)
from src.domain.schemas import ClienteCreate, InstalacionRequest
from src.infrastructure.repositories import ClienteRepository

# Servicios Externos
from src.infrastructure.mikrotik_service import MikroTikService

# 👇 Importamos el MEGA NOTIFICADOR 👇
#from src.application.helpers.notification_manager import enviar_notificacion_automatica
from src.application.services.notification_service import NotificationService
from src.application.services.ftth_service import FTTHService
from src.application.services.finance_service import FinanceService
from src.application.services.ipam_service import IPAMService
from src.application.services.access_control_service import (
    verificar_instalacion_asignada,
)

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
            .order_by(ServicioModel.id.asc())
        )

        resultado = await self.db.execute(stmt)
        servicio = resultado.scalars().first()
        if servicio:
            return servicio

        servicio = ServicioModel(
            cliente_id=cliente.id,
            alias="Principal",
            direccion=cliente.direccion,
            latitud=cliente.latitud,
            longitud=cliente.longitud,
            router_id=cliente.router_id,
            plan_id=cliente.plan_id,
            plantilla_id=cliente.plantilla_id,
            zona_id=cliente.zona_id,
            red_id=cliente.red_id,
            olt_id=cliente.olt_id,
            caja_nap_id=cliente.caja_nap_id,
            puerto_nap=cliente.puerto_nap,
            tecnico_id=cliente.tecnico_id,
            onu_id=cliente.onu_id,
            ip_asignada=cliente.ip_asignada,
            mac_address=cliente.mac_address,
            user_pppoe=cliente.user_pppoe,
            pass_pppoe=cliente.pass_pppoe,
            is_online=cliente.is_online,
            ultimo_cambio_estado=cliente.ultimo_cambio_estado,
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
    async def registrar_cliente(
        self,
        datos: ClienteCreate,
        background_tasks: BackgroundTasks,
        usuario_operador=None,
    ):
        """
        Crea la orden en BD, cambia el estado del equipo en Inventario a 'INSTALADO'
        y genera un ID Hexadecimal Aleatorio.
        """
        import random
        import string
        from sqlalchemy.exc import IntegrityError 

        if datos.nombre:
            datos.nombre = datos.nombre.strip()
        if datos.telefono:
            datos.telefono = datos.telefono.strip()

        plan = (
            await self.db.get(PlanModel, datos.plan_id)
            if datos.plan_id
            else None
        )
        if datos.plan_id and not plan:
            raise ValueError("El plan seleccionado no existe")
        if plan and datos.router_id and plan.router_id != datos.router_id:
            raise ValueError(
                "El plan seleccionado no pertenece al router indicado"
            )

        olt = (
            await self.db.get(OLTModel, datos.olt_id)
            if datos.olt_id
            else None
        )
        if datos.olt_id and not olt:
            raise ValueError("La OLT seleccionada no existe")
        if olt and datos.router_id and olt.router_id != datos.router_id:
            raise ValueError(
                "La OLT seleccionada no pertenece al router indicado"
            )

        caja = (
            await self.db.get(CajaNapModel, datos.caja_nap_id)
            if datos.caja_nap_id
            else None
        )
        if datos.caja_nap_id and not caja:
            raise ValueError("La caja NAP seleccionada no existe")
        if caja and datos.zona_id and caja.zona_id != datos.zona_id:
            raise ValueError(
                "La caja NAP seleccionada no pertenece a la zona indicada"
            )
        if caja and datos.olt_id and caja.olt_id:
            if caja.olt_id != datos.olt_id:
                raise ValueError(
                    "La caja NAP seleccionada no pertenece a la OLT indicada"
                )
        if caja and caja.olt_id and datos.router_id:
            olt_caja = await self.db.get(OLTModel, caja.olt_id)
            if olt_caja and olt_caja.router_id != datos.router_id:
                raise ValueError(
                    "La caja NAP seleccionada no pertenece al router indicado"
                )

        # Un abonado con otra vivienda conserva el mismo cliente y recibe
        # un servicio adicional. Evitamos repetir por accidente una misma
        # alta cuando el frontend reintenta una solicitud ya confirmada.
        if datos.nombre and datos.telefono:
            cliente_duplicado = (
                await self.db.execute(
                    select(ClienteModel).where(
                        func.lower(func.trim(ClienteModel.nombre))
                        == datos.nombre.casefold(),
                        ClienteModel.telefono == datos.telefono,
                    )
                )
            ).scalar_one_or_none()
            if cliente_duplicado:
                raise ValueError(
                    f"Ya existe el cliente {cliente_duplicado.nombre} "
                    f"(ID {cliente_duplicado.id}) con este teléfono. "
                    "Si es otro domicilio, agrega un servicio al cliente "
                    "existente."
                )

        # A. Generar Credenciales Automáticas
        if not datos.user_pppoe and datos.nombre:
            base = datos.nombre.lower().replace(" ", "")[:8]
            rand = random.randint(100, 999)
            datos.user_pppoe = f"{base}{rand}"
        
        if not datos.pass_pppoe:
            datos.pass_pppoe = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))

        # B. Manejo de IP
        ip_limpia = datos.ip_asignada.strip() if datos.ip_asignada else None
        if ip_limpia == "0.0.0.0":
            ip_limpia = None

        if datos.red_id:
            # La reserva se realiza en el backend y bajo bloqueo de la red.
            # Si el frontend no eligió una IP, toma automáticamente la primera.
            datos.ip_asignada = await IPAMService(
                self.db
            ).reservar_para_cliente(
                red_id=datos.red_id,
                ip_solicitada=ip_limpia,
                router_id=datos.router_id,
            )
        elif not ip_limpia:
            datos.ip_asignada = None
        else:
            stmt = select(ClienteModel).where(
                ClienteModel.ip_asignada == ip_limpia,
            )
            existing = await self.db.execute(stmt)
            ocupante = existing.scalar_one_or_none()
            if ocupante:
                raise ValueError(f"La IP {ip_limpia} ya la tiene: {ocupante.nombre}")
            datos.ip_asignada = ip_limpia

        # D. Preparamos el objeto para la Base de Datos
        datos_dict = (
            datos.model_dump(exclude={"cedula"})
            if hasattr(datos, "model_dump")
            else datos.dict(exclude={"cedula"})
        )
        
        # Convertir 0 o "" a None en llaves foráneas
        for fk in ["olt_id", "onu_id", "router_id", "plan_id", "zona_id", "plantilla_id", "caja_nap_id", "tecnico_id", "red_id"]:
            if datos_dict.get(fk) in [0, "0", ""]:
                datos_dict[fk] = None

        datos_dict['estado'] = "pendiente_instalacion"

        onu_reservada = None
        try:
            if datos_dict.get("onu_id"):
                onu_reservada = (
                    await self.db.execute(
                        select(InventarioONUModel)
                        .where(
                            InventarioONUModel.id == datos_dict["onu_id"]
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if not onu_reservada:
                    raise ValueError("La ONU seleccionada no existe")

                cliente_ocupante = (
                    await self.db.execute(
                        select(ClienteModel).where(
                            ClienteModel.onu_id == onu_reservada.id
                        )
                    )
                ).scalar_one_or_none()
                if cliente_ocupante:
                    raise ValueError(
                        f"La ONU {onu_reservada.identificador} ya está "
                        f"asignada a {cliente_ocupante.nombre} "
                        f"(cliente {cliente_ocupante.id})"
                    )

                servicio_ocupante = (
                    await self.db.execute(
                        select(ServicioModel).where(
                            ServicioModel.onu_id == onu_reservada.id
                        )
                    )
                ).scalar_one_or_none()
                if servicio_ocupante:
                    raise ValueError(
                        f"La ONU {onu_reservada.identificador} ya está "
                        f"asignada al servicio {servicio_ocupante.id}"
                    )

                if onu_reservada.estado != "DISPONIBLE":
                    raise ValueError(
                        f"El equipo {onu_reservada.identificador} no está "
                        f"disponible (estado {onu_reservada.estado})"
                    )
        except Exception:
            # IPAM puede haber tomado un bloqueo antes de validar la ONU.
            # Se libera toda la transacción si la selección es inválida.
            await self.db.rollback()
            raise

        # 🔥 AQUÍ ESTÁ EL ESCUDO: Quitamos la basura virtual antes de guardar 🔥
        datos_dict.pop("identificador_onu", None)
        datos_dict.pop("mac_address", None)

        # 🚀 Ahora sí, la base de datos lo aceptará sin quejarse
        nuevo_cliente = ClienteModel(**datos_dict)
        self.db.add(nuevo_cliente)

        try:
            await self.db.flush() 
            
            # Reservar la ONU hasta que el técnico complete la instalación.
            if nuevo_cliente.onu_id:
                onu = onu_reservada
                if onu:
                    onu.estado = "RESERVADO"
                    if nuevo_cliente.tecnico_id:
                        onu.tecnico_id = nuevo_cliente.tecnico_id
                    FTTHService(self.db).registrar_movimiento(
                        onu=onu,
                        cliente_id=nuevo_cliente.id,
                        tecnico_id=(
                            usuario_operador.id
                            if usuario_operador
                            else nuevo_cliente.tecnico_id
                        ),
                        tipo_movimiento="reserva",
                        estado_anterior="DISPONIBLE",
                        estado_nuevo="RESERVADO",
                        motivo="Reservada para instalación pendiente",
                    )
            
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
            servicio_principal = await self._obtener_o_crear_servicio(
                nuevo_cliente
            )
            # La instalación deja de representarse únicamente como cliente.
            estado_orden = (
                "asignada" if nuevo_cliente.tecnico_id else "pendiente"
            )
            orden = OrdenServicioModel(
                tipo="instalacion",
                cliente_id=nuevo_cliente.id,
                servicio_id=servicio_principal.id,
                tecnico_id=nuevo_cliente.tecnico_id,
                creado_por_id=(
                    usuario_operador.id if usuario_operador else None
                ),
                prioridad="normal",
                estado=estado_orden,
                motivo="Nueva instalación",
                descripcion="Orden creada al registrar el cliente",
            )
            self.db.add(orden)
            await self.db.flush()
            self.db.add(
                HistorialEstadoOrdenModel(
                    orden_id=orden.id,
                    usuario_id=(
                        usuario_operador.id if usuario_operador else None
                    ),
                    estado_anterior=None,
                    estado_nuevo=estado_orden,
                    comentario="Orden creada desde alta de cliente",
                )
            )
            # F. Guardamos permanentemente
            await self.db.commit()
            await self.db.refresh(nuevo_cliente)
            
            return await self._recargar_cliente(nuevo_cliente.id)
            
        except IntegrityError as e:
            await self.db.rollback()
            print(f"Error de Integridad DB: {e}")
            detalle = str(getattr(e, "orig", e))
            if "uq_clientes_onu_id" in detalle:
                mensaje = (
                    "La ONU seleccionada ya fue asignada a otro cliente. "
                    "Actualiza el inventario y elige otra."
                )
            elif "uq_clientes_nap_puerto" in detalle:
                mensaje = (
                    "El puerto NAP seleccionado ya está ocupado por "
                    "otro cliente."
                )
            elif "ip_asignada" in detalle:
                mensaje = "La IP seleccionada ya está ocupada."
            else:
                mensaje = (
                    "No se pudo registrar porque uno de los datos "
                    "seleccionados ya está en uso."
                )
            raise ValueError(mensaje) from e
        except ValueError:
            await self.db.rollback()
            raise
        except Exception as e:
            await self.db.rollback()
            print(f"Error DB Registrar: {e}")
            raise Exception(f"Error al generar cliente: {e}")

    # ==========================================
    # 2. ACTIVAR INSTALACIÓN (VERSION FINAL - VARIABLES SEGURAS)
    # ==========================================
    async def activar_instalacion(
        self,
        cliente_id: int,
        datos_finales: InstalacionRequest,
        usuario_operador=None,
        orden_id: int = None,
    ):
        """
        Activa el servicio en Mikrotik. Si el técnico cambia la ONU, actualiza el inventario.
        """
        # A. Recuperar Cliente
        cliente = await self.db.get(ClienteModel, cliente_id)
        if not cliente: raise ValueError("Cliente no encontrado")
        if usuario_operador is not None:
            await verificar_instalacion_asignada(
                self.db,
                usuario_operador,
                cliente_id,
            )

        if datos_finales.cedula is not None:
            cliente.cedula = datos_finales.cedula
            
        if hasattr(datos_finales, 'olt_id') and datos_finales.olt_id is not None:
            cliente.olt_id = datos_finales.olt_id
            
        operador_id = (
            usuario_operador.id
            if usuario_operador
            else cliente.tecnico_id
        )
        if not operador_id:
            raise ValueError("No se pudo identificar al operador de instalación")
        ftth_service = FTTHService(self.db)

        orden_instalacion = None
        if orden_id is not None:
            orden_instalacion = await self.db.get(
                OrdenServicioModel,
                orden_id,
            )
            if (
                not orden_instalacion
                or orden_instalacion.cliente_id != cliente.id
                or orden_instalacion.tipo != "instalacion"
            ):
                raise ValueError(
                    "La orden indicada no corresponde a esta instalación"
                )
        else:
            orden_instalacion = (
                await self.db.execute(
                    select(OrdenServicioModel)
                    .where(
                        OrdenServicioModel.cliente_id == cliente.id,
                        OrdenServicioModel.tipo == "instalacion",
                        OrdenServicioModel.estado.in_(
                            {
                                "pendiente",
                                "asignada",
                                "en_camino",
                                "trabajando",
                            }
                        ),
                    )
                    .order_by(OrdenServicioModel.id.desc())
                    .with_for_update()
                )
            ).scalars().first()
            if orden_instalacion:
                orden_id = orden_instalacion.id

        # Asignación transaccional con bloqueo y trazabilidad.
        if hasattr(datos_finales, 'onu_id') and datos_finales.onu_id is not None:
            await ftth_service.asignar_onu(
                cliente,
                datos_finales.onu_id,
                operador_id,
                orden_id=orden_id,
                motivo="Instalación de servicio",
                potencia_dbm=(
                    Decimal(str(datos_finales.potencia_optica_dbm))
                    if datos_finales.potencia_optica_dbm is not None
                    else None
                ),
            )

        # PROTECCIÓN CLAVE: Solo actualizar si el técnico lo envió
        if datos_finales.router_id is not None: cliente.router_id = datos_finales.router_id
        if datos_finales.plan_id is not None: cliente.plan_id = datos_finales.plan_id

        plan = (
            await self.db.get(PlanModel, cliente.plan_id)
            if cliente.plan_id
            else None
        )
        if cliente.plan_id and not plan:
            raise ValueError("El plan seleccionado no existe")
        if plan and cliente.router_id and plan.router_id != cliente.router_id:
            raise ValueError(
                "El plan seleccionado no pertenece al router indicado"
            )
        olt = (
            await self.db.get(OLTModel, cliente.olt_id)
            if cliente.olt_id
            else None
        )
        if cliente.olt_id and not olt:
            raise ValueError("La OLT seleccionada no existe")
        if olt and cliente.router_id and olt.router_id != cliente.router_id:
            raise ValueError(
                "La OLT seleccionada no pertenece al router indicado"
            )
        
        # INFRAESTRUCTURA FIBRA Y GPS
        caja_nap_id = datos_finales.caja_nap_id or cliente.caja_nap_id
        puerto_nap = datos_finales.puerto_nap or cliente.puerto_nap
        caja = (
            await self.db.get(CajaNapModel, caja_nap_id)
            if caja_nap_id
            else None
        )
        if caja_nap_id and not caja:
            raise ValueError("La caja NAP seleccionada no existe")
        if caja and cliente.zona_id and caja.zona_id != cliente.zona_id:
            raise ValueError(
                "La caja NAP seleccionada no pertenece a la zona indicada"
            )
        if caja and cliente.olt_id and caja.olt_id:
            if caja.olt_id != cliente.olt_id:
                raise ValueError(
                    "La caja NAP seleccionada no pertenece a la OLT indicada"
                )
        if caja and caja.olt_id and cliente.router_id:
            olt_caja = await self.db.get(OLTModel, caja.olt_id)
            if olt_caja and olt_caja.router_id != cliente.router_id:
                raise ValueError(
                    "La caja NAP seleccionada no pertenece al router indicado"
                )
        if bool(caja_nap_id) != bool(puerto_nap):
            raise ValueError("Debes indicar la caja NAP y el puerto")
        if caja_nap_id and puerto_nap:
            await ftth_service.asignar_puerto(
                cliente,
                caja_nap_id,
                puerto_nap,
                operador_id,
                orden_id=orden_id,
                potencia_dbm=(
                    Decimal(str(datos_finales.potencia_optica_dbm))
                    if datos_finales.potencia_optica_dbm is not None
                    else None
                ),
            )
        if datos_finales.latitud is not None: cliente.latitud = datos_finales.latitud
        if datos_finales.longitud is not None: cliente.longitud = datos_finales.longitud

        if cliente.latitud is None or cliente.longitud is None:
            raise ValueError(
                "Debes capturar la ubicación GPS antes de activar "
                "la instalación"
            )
        if not (-90 <= cliente.latitud <= 90) or not (
            -180 <= cliente.longitud <= 180
        ):
            raise ValueError("Las coordenadas GPS no son válidas")
        if cliente.latitud == 0 and cliente.longitud == 0:
            raise ValueError(
                "Las coordenadas 0,0 no son una ubicación válida "
                "para el cliente"
            )
        
        # Gestión de IP
        ip_para_mikrotik = None
        if datos_finales.ip_asignada and datos_finales.ip_asignada != '0.0.0.0':
            ip_limpia = str(datos_finales.ip_asignada).strip()

            if cliente.red_id:
                ip_limpia = await IPAMService(
                    self.db
                ).reservar_para_cliente(
                    red_id=cliente.red_id,
                    ip_solicitada=ip_limpia,
                    router_id=cliente.router_id,
                    excluir_cliente_id=cliente.id,
                )
            else:
                stmt_ip = select(ClienteModel).where(
                    ClienteModel.ip_asignada == ip_limpia,
                    ClienteModel.id != cliente.id,
                    ClienteModel.estado != "eliminado",
                )
                res_ip = await self.db.execute(stmt_ip)
                ocupante = res_ip.scalar_one_or_none()
                if ocupante:
                    raise ValueError(
                        f"La IP {ip_limpia} ya está asignada a otro cliente: "
                        f"{ocupante.nombre} (ID {ocupante.id})"
                    )

            cliente.ip_asignada = ip_limpia
            ip_para_mikrotik = cliente.ip_asignada
        elif cliente.ip_asignada:
            ip_para_mikrotik = cliente.ip_asignada
        elif cliente.red_id:
            cliente.ip_asignada = await IPAMService(
                self.db
            ).reservar_para_cliente(
                red_id=cliente.red_id,
                router_id=cliente.router_id,
                excluir_cliente_id=cliente.id,
            )
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

        if datos_finales.potencia_optica_dbm is not None:
            await ftth_service.registrar_lectura_optica(
                cliente,
                Decimal(str(datos_finales.potencia_optica_dbm)),
                operador_id,
                potencia_tx_dbm=(
                    Decimal(str(datos_finales.potencia_tx_dbm))
                    if datos_finales.potencia_tx_dbm is not None
                    else None
                ),
                orden_id=orden_id,
                origen="manual",
                observaciones=datos_finales.observaciones_opticas,
            )

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
        servicio.alias = servicio.alias or "Principal"
        servicio.direccion = cliente.direccion
        servicio.latitud = cliente.latitud
        servicio.longitud = cliente.longitud
        servicio.router_id = cliente.router_id
        servicio.zona_id = cliente.zona_id
        servicio.red_id = cliente.red_id
        servicio.olt_id = cliente.olt_id
        servicio.caja_nap_id = cliente.caja_nap_id
        servicio.puerto_nap = cliente.puerto_nap
        servicio.tecnico_id = cliente.tecnico_id
        servicio.onu_id = cliente.onu_id
        servicio.ip_asignada = cliente.ip_asignada
        servicio.mac_address = cliente.mac_address
        servicio.user_pppoe = cliente.user_pppoe
        servicio.pass_pppoe = cliente.pass_pppoe
        servicio.is_online = cliente.is_online
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

        if orden_instalacion:
            estado_anterior_orden = orden_instalacion.estado
            orden_instalacion.servicio_id = servicio.id
            orden_instalacion.estado = "terminada"
            orden_instalacion.fecha_inicio = (
                orden_instalacion.fecha_inicio or datetime.now()
            )
            orden_instalacion.fecha_finalizacion = datetime.now()
            orden_instalacion.version += 1
            self.db.add(
                HistorialEstadoOrdenModel(
                    orden_id=orden_instalacion.id,
                    usuario_id=operador_id,
                    estado_anterior=estado_anterior_orden,
                    estado_nuevo="terminada",
                    comentario=(
                        "Instalación completada mediante activación directa"
                    ),
                )
            )
        if servicio.caja_nap_id and servicio.puerto_nap:
            puerto = (
                await self.db.execute(
                    select(PuertoNapModel).where(
                        PuertoNapModel.cliente_id == cliente.id,
                        PuertoNapModel.caja_nap_id
                        == servicio.caja_nap_id,
                        PuertoNapModel.numero == servicio.puerto_nap,
                    )
                )
            ).scalar_one_or_none()
            if puerto:
                puerto.servicio_id = servicio.id

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

        router_objetivo = update_data.get(
            "router_id",
            cliente_db.router_id,
        )
        plan_objetivo = update_data.get("plan_id", cliente_db.plan_id)
        zona_objetivo = update_data.get("zona_id", cliente_db.zona_id)
        olt_objetivo = update_data.get("olt_id", cliente_db.olt_id)
        nap_objetiva = update_data.get(
            "caja_nap_id",
            cliente_db.caja_nap_id,
        )

        tiene_servicios = (
            await self.db.execute(
                select(func.count(ServicioModel.id)).where(
                    ServicioModel.cliente_id == cliente_id,
                    ServicioModel.estado != "cancelado",
                )
            )
        ).scalar_one() > 0
        if (
            tiene_servicios
            and "plan_id" in update_data
            and plan_objetivo != cliente_db.plan_id
        ):
            raise ValueError(
                "El plan pertenece a cada contrato. Cámbialo desde la "
                "pestaña Servicios y selecciona el domicilio correcto."
            )
        if (
            tiene_servicios
            and "router_id" in update_data
            and router_objetivo != cliente_db.router_id
        ):
            raise ValueError(
                "El router pertenece a cada contrato. Realiza el cambio "
                "desde el servicio o mediante una migración técnica."
            )

        plan = (
            await self.db.get(PlanModel, plan_objetivo)
            if plan_objetivo
            else None
        )
        if plan_objetivo and not plan:
            raise ValueError("El plan seleccionado no existe")
        if plan and router_objetivo and plan.router_id != router_objetivo:
            raise ValueError(
                "El plan seleccionado no pertenece al router indicado"
            )

        olt = (
            await self.db.get(OLTModel, olt_objetivo)
            if olt_objetivo
            else None
        )
        if olt_objetivo and not olt:
            raise ValueError("La OLT seleccionada no existe")
        if olt and router_objetivo and olt.router_id != router_objetivo:
            raise ValueError(
                "La OLT seleccionada no pertenece al router indicado"
            )

        caja = (
            await self.db.get(CajaNapModel, nap_objetiva)
            if nap_objetiva
            else None
        )
        if nap_objetiva and not caja:
            raise ValueError("La caja NAP seleccionada no existe")
        if caja and zona_objetivo and caja.zona_id != zona_objetivo:
            raise ValueError(
                "La caja NAP seleccionada no pertenece a la zona indicada"
            )
        if caja and olt_objetivo and caja.olt_id:
            if caja.olt_id != olt_objetivo:
                raise ValueError(
                    "La caja NAP seleccionada no pertenece a la OLT indicada"
                )
        if caja and caja.olt_id and router_objetivo:
            olt_caja = await self.db.get(OLTModel, caja.olt_id)
            if olt_caja and olt_caja.router_id != router_objetivo:
                raise ValueError(
                    "La caja NAP seleccionada no pertenece al router indicado"
                )

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
        cantidad_servicios = (
            await self.db.execute(
                select(func.count(ServicioModel.id)).where(
                    ServicioModel.cliente_id == cliente_id,
                    ServicioModel.estado != "cancelado",
                )
            )
        ).scalar_one()
        if cantidad_servicios > 1:
            raise ValueError(
                "El cliente tiene varios servicios; cambia el estado "
                "por servicio_id para no afectar otros domicilios"
            )

        estado_limpio = nuevo_estado.lower().strip()
        await self.db.refresh(cliente, attribute_names=['router'])

        if estado_limpio in {
            "activo",
            "suspendido",
            "retirado",
            "cortado",
        }:
            if not cliente.router or not cliente.ip_asignada:
                raise ValueError(
                    "El cliente necesita router e IP para sincronizar "
                    "su estado con MikroTik"
                )

            mk = MikroTikService(
                cliente.router.ip_vpn,
                cliente.router.user_api,
                cliente.router.pass_api,
                cliente.router.port_api,
            )

            try:
                if estado_limpio in ["suspendido", "retirado", "cortado"]:
                    resultado = mk.gestionar_corte_cliente(
                        cliente.ip_asignada,
                        suspender=True,
                    )
                    if resultado is not True:
                        raise RuntimeError(
                            "MikroTik no confirmó la suspensión"
                        )
                    if cliente.user_pppoe:
                        mk.desconectar_cliente_activo(
                            cliente.user_pppoe,
                        )
                elif estado_limpio == "activo":
                    resultado = mk.reactivar_cliente(
                        cliente.ip_asignada,
                        cliente.user_pppoe,
                    )
                    if resultado is not True:
                        raise RuntimeError(
                            "MikroTik no confirmó la reactivación"
                        )
            except Exception as exc:
                await self.db.rollback()
                raise ValueError(
                    f"No se cambió el estado: {exc}"
                ) from exc

        cliente.estado = estado_limpio
        await self.db.execute(
            update(ServicioModel)
            .where(
                ServicioModel.cliente_id == cliente.id,
                ServicioModel.estado != "cancelado",
            )
            .values(estado=estado_limpio)
        )
        await self.db.commit()

        if cliente.telefono:
            notificador = NotificationService(self.db)
            if estado_limpio in ["suspendido", "retirado", "cortado"]:
                await notificador.notificar(
                    "corte_servicio",
                    cliente.id,
                )
            elif estado_limpio == "activo":
                await notificador.notificar(
                    "reconexion",
                    cliente.id,
                )

        return f"Cliente {estado_limpio}"

    # ==========================================
    # 5. ELIMINAR CLIENTE
    # ==========================================
    async def eliminar_cliente(self, cliente_id: int):
        """
        Eliminación física del cliente.

        Para baja comercial o suspensión se debe usar Cancelar servicio.
        Esta acción elimina el registro del cliente de la plataforma y limpia
        primero las tablas relacionadas para evitar errores de FK/cliente_id.
        """
        cliente = await self.db.get(ClienteModel, cliente_id)
        if not cliente:
            raise ValueError("Cliente no encontrado")

        # 1. Liberar ONU del inventario
        if cliente.onu_id:
            onu = await self.db.get(InventarioONUModel, cliente.onu_id)
            if onu:
                onu.estado = "DISPONIBLE"
                onu.tecnico_id = None
                print(f"📦 ONU {onu.identificador} regresada a DISPONIBLE.")

        # 2. Eliminar PPPoE del MikroTik si existe
        if cliente.router_id and cliente.user_pppoe:
            try:
                router = await self.db.get(RouterModel, cliente.router_id)
                if router:
                    mk = MikroTikService(
                        router.ip_vpn,
                        router.user_api,
                        router.pass_api,
                        router.port_api,
                    )
                    mk.eliminar_pppoe_user(cliente.user_pppoe)
            except Exception as e:
                print(f"⚠️ No se pudo eliminar PPPoE en MikroTik: {e}")

        # 3. Eliminación definitiva: se borran los datos del cliente y sus
        # historiales. La baja de servicio queda como estado reversible;
        # este endpoint es la acción explícita para purgar un registro.
        await self.db.execute(text("DELETE FROM bajas_servicio WHERE cliente_id = :cliente_id"), {"cliente_id": cliente_id})
        await self.db.execute(text("DELETE FROM promesas_pago_historial WHERE cliente_id = :cliente_id"), {"cliente_id": cliente_id})
        await self.db.execute(text("DELETE FROM diagnosticos_soporte WHERE cliente_id = :cliente_id"), {"cliente_id": cliente_id})
        await self.db.execute(text("DELETE FROM lecturas_opticas WHERE cliente_id = :cliente_id"), {"cliente_id": cliente_id})
        await self.db.execute(text("DELETE FROM ordenes_servicio WHERE cliente_id = :cliente_id"), {"cliente_id": cliente_id})
        await self.db.execute(text("DELETE FROM pagos_autovalidados WHERE cliente_id = :cliente_id"), {"cliente_id": cliente_id})
        await self.db.execute(
            text("DELETE FROM descuentos_factura WHERE factura_id IN (SELECT id FROM facturas WHERE cliente_id = :cliente_id)"),
            {"cliente_id": cliente_id},
        )
        await self.db.execute(
            text("DELETE FROM pagos WHERE cliente_id = :cliente_id"),
            {"cliente_id": cliente_id},
        )

        await self.db.execute(
            text("DELETE FROM facturas WHERE cliente_id = :cliente_id"),
            {"cliente_id": cliente_id},
        )

        await self.db.execute(
            text("DELETE FROM servicios WHERE cliente_id = :cliente_id"),
            {"cliente_id": cliente_id},
        )

        await self.db.execute(
            text("DELETE FROM mensajes_chat WHERE cliente_id = :cliente_id"),
            {"cliente_id": cliente_id},
        )

        await self.db.execute(
            text("DELETE FROM pagos_autovalidados WHERE cliente_id = :cliente_id"),
            {"cliente_id": cliente_id},
        )

        await self.db.delete(cliente)
        await self.db.commit()

        return "Cliente eliminado definitivamente y equipo liberado correctamente"

    # ==========================================
    # 5. PROMESA D EPAGO
    # ==========================================
    async def registrar_promesa_pago(
        self,
        cliente_id: int,
        fecha_promesa: date,
        usuario_id: int | None = None,
    ):
        """
        Registra una promesa de pago sobre la deuda más antigua del cliente.

        Funciona para facturas pendientes, vencidas o ya marcadas como promesa.
        Si el cliente estaba suspendido, intenta reactivarlo y deja el servicio activo.
        """
        stmt_f = select(FacturaModel).where(
            FacturaModel.cliente_id == cliente_id,
            FacturaModel.estado.in_(["pendiente", "vencida"]),
            FacturaModel.saldo_pendiente > 0,
        ).order_by(FacturaModel.fecha_vencimiento.asc())

        res_f = await self.db.execute(stmt_f)
        factura = res_f.scalars().first()

        if not factura:
            raise ValueError("El cliente no tiene facturas con saldo pendiente para aplicar promesa.")

        cliente = await self.db.get(ClienteModel, cliente_id)
        if not cliente:
            raise ValueError("Cliente no encontrado")

        from src.application.services.billing_service import BillingService

        promesa, factura, cliente, politica, reactivado = (
            await BillingService(self.db).registrar_promesa_y_reactivar(
                factura.id,
                fecha_promesa,
                usuario_id,
            )
        )

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
    # 🔥 NUEVA FUNCIÓN: SWAP DE ONU POR FALLA 🔥
    async def procesar_cambio_onu(
        self,
        cliente_id: int,
        nuevo_inventario_id: int,
        estado_vieja_onu: str,
        usuario_operador=None,
        orden_id: int = None,
    ):
        # 1. Validaciones iniciales
        cliente = await self.db.get(ClienteModel, cliente_id)
        if not cliente:
            raise ValueError("Cliente no encontrado")

        estado_limpio = estado_vieja_onu.upper().strip()
        if estado_limpio in ["DAÑADA", "DANADA", "DAÑADO", "DANADO", "CON_FALLA", "FALLA"]:
            estado_limpio = "CON_FALLA"

        operador_id = (
            usuario_operador.id
            if usuario_operador
            else cliente.tecnico_id
        )
        if not operador_id:
            raise ValueError("No se pudo identificar al operador del cambio")

        onu_nueva = await FTTHService(self.db).asignar_onu(
            cliente,
            nuevo_inventario_id,
            operador_id,
            orden_id=orden_id,
            motivo="Cambio de ONU",
            estado_onu_anterior=estado_limpio,
            condicion_onu_anterior=estado_limpio,
        )
        await self.db.commit()

        return f"Cambio exitoso. Nueva ONU asignada: {onu_nueva.identificador}."
