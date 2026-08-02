import asyncio
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload
from sqlalchemy.orm.attributes import NO_VALUE, instance_state

from src.application.services.billing_calendar_service import (
    BillingCalendarService,
)
from src.application.services.ftth_service import FTTHService
from src.application.services.ipam_service import IPAMService
from src.domain.schemas import (
    ServicioActivacion,
    ServicioCreate,
    ServicioPlanUpdate,
    ServicioUpdate,
)
from src.infrastructure.mikrotik_service import MikroTikService
from src.infrastructure.models import (
    CajaNapModel,
    CicloFacturacion,
    ClienteModel,
    LogCronjobModel,
    OLTModel,
    OrdenServicioModel,
    PlanModel,
    PlantillaFacturacionModel,
    RedModel,
    RouterModel,
    ServicioModel,
    TipoFacturacion,
)


class SubscriptionService:
    """Gestiona cada contrato/domicilio sin duplicar a la persona."""

    def __init__(self, db: AsyncSession):
        self.db = db

    @staticmethod
    def _consulta_relaciones():
        return select(ServicioModel).options(
            joinedload(ServicioModel.cliente),
            joinedload(ServicioModel.router),
            joinedload(ServicioModel.plan),
            joinedload(ServicioModel.plantilla),
            joinedload(ServicioModel.zona),
            joinedload(ServicioModel.red),
            joinedload(ServicioModel.olt),
            joinedload(ServicioModel.caja_nap),
            joinedload(ServicioModel.onu),
        )

    async def obtener(self, servicio_id: int) -> ServicioModel:
        servicio = (
            await self.db.execute(
                self._consulta_relaciones().where(
                    ServicioModel.id == servicio_id
                )
            )
        ).scalar_one_or_none()
        if not servicio:
            raise ValueError("Servicio no encontrado")
        return servicio

    async def listar_cliente(self, cliente_id: int) -> list[ServicioModel]:
        return (
            await self.db.execute(
                self._consulta_relaciones()
                .where(ServicioModel.cliente_id == cliente_id)
                .order_by(ServicioModel.id)
            )
        ).scalars().unique().all()

    async def crear(
        self,
        datos: ServicioCreate,
        usuario_id: int,
    ) -> ServicioModel:
        cliente = await self.db.get(ClienteModel, datos.cliente_id)
        if not cliente:
            raise ValueError("Cliente no encontrado")

        duplicado = (
            await self.db.execute(
                select(ServicioModel.id).where(
                    ServicioModel.cliente_id == cliente.id,
                    func.lower(ServicioModel.alias)
                    == datos.alias.strip().lower(),
                    ServicioModel.estado != "cancelado",
                )
            )
        ).scalar_one_or_none()
        if duplicado:
            raise ValueError(
                "El cliente ya tiene un servicio con ese alias"
            )

        await self._validar_catalogos(
            router_id=datos.router_id,
            plan_id=(
                datos.plan_id
                if datos.plan_id is not None
                else cliente.plan_id
            ),
            # Compatibilidad con clientes existentes: si el nuevo domicilio
            # no especifica plantilla, hereda la del cliente.
            plantilla_id=(
                datos.plantilla_id
                if datos.plantilla_id is not None
                else cliente.plantilla_id
            ),
            red_id=datos.red_id,
        )

        servicio = ServicioModel(
            cliente_id=cliente.id,
            alias=datos.alias.strip(),
            direccion=datos.direccion.strip(),
            latitud=datos.latitud,
            longitud=datos.longitud,
            router_id=datos.router_id,
            plan_id=(
                datos.plan_id
                if datos.plan_id is not None
                else cliente.plan_id
            ),
            plantilla_id=(
                datos.plantilla_id
                if datos.plantilla_id is not None
                else cliente.plantilla_id
            ),
            zona_id=datos.zona_id,
            red_id=datos.red_id,
            tecnico_id=datos.tecnico_id,
            tipo_facturacion=TipoFacturacion(
                datos.tipo_facturacion.value
            ),
            ciclo_facturacion=CicloFacturacion(
                datos.ciclo_facturacion.value
            ),
            meses_gratis=datos.meses_gratis,
            estado="pendiente_instalacion",
        )
        self.db.add(servicio)
        await self.db.flush()

        if datos.crear_orden:
            self.db.add(
                OrdenServicioModel(
                    tipo="instalacion",
                    cliente_id=cliente.id,
                    servicio_id=servicio.id,
                    tecnico_id=datos.tecnico_id,
                    creado_por_id=usuario_id,
                    prioridad="normal",
                    estado="pendiente",
                    motivo="Instalación de nuevo servicio",
                    descripcion=(
                        f"{servicio.alias}: {servicio.direccion}"
                    ),
                )
            )

        await self._sincronizar_estado_cliente(cliente.id)
        await self.db.commit()
        return await self.obtener(servicio.id)

    async def actualizar(
        self,
        servicio_id: int,
        datos: ServicioUpdate,
    ) -> ServicioModel:
        servicio = await self.obtener(servicio_id)
        cambios = datos.model_dump(exclude_unset=True)
        if servicio.estado in {"activo", "suspendido"}:
            topologia = {"router_id", "plan_id", "red_id"}
            cambios_topologia = {
                campo
                for campo in topologia
                if campo in cambios
                and cambios[campo] != getattr(servicio, campo)
            }
            if cambios_topologia:
                raise ValueError(
                    "No cambies router, plan o red de un servicio instalado "
                    "desde la edición general; usa un flujo técnico"
                )
        if "alias" in cambios and cambios["alias"] is not None:
            cambios["alias"] = cambios["alias"].strip()
            duplicado = (
                await self.db.execute(
                    select(ServicioModel.id).where(
                        ServicioModel.cliente_id
                        == servicio.cliente_id,
                        func.lower(ServicioModel.alias)
                        == cambios["alias"].lower(),
                        ServicioModel.id != servicio.id,
                        ServicioModel.estado != "cancelado",
                    )
                )
            ).scalar_one_or_none()
            if duplicado:
                raise ValueError(
                    "El cliente ya tiene un servicio con ese alias"
                )
        if "direccion" in cambios and cambios["direccion"] is not None:
            cambios["direccion"] = cambios["direccion"].strip()
        if "tipo_facturacion" in cambios:
            cambios["tipo_facturacion"] = TipoFacturacion(
                cambios["tipo_facturacion"].value
            )
        if "ciclo_facturacion" in cambios:
            cambios["ciclo_facturacion"] = CicloFacturacion(
                cambios["ciclo_facturacion"].value
            )

        await self._validar_catalogos(
            router_id=cambios.get("router_id", servicio.router_id),
            plan_id=cambios.get("plan_id", servicio.plan_id),
            plantilla_id=cambios.get(
                "plantilla_id",
                servicio.plantilla_id,
            ),
            red_id=cambios.get("red_id", servicio.red_id),
        )
        for campo, valor in cambios.items():
            setattr(servicio, campo, valor)
        await self.db.commit()
        return await self.obtener(servicio.id)

    async def activar(
        self,
        servicio_id: int,
        datos: ServicioActivacion,
        usuario_id: int,
    ) -> ServicioModel:
        servicio = await self.obtener(servicio_id)
        if servicio.estado == "cancelado":
            raise ValueError(
                "Un servicio cancelado debe reactivarse desde su expediente"
            )
        if (
            servicio.estado in {"activo", "suspendido"}
            and datos.router_id is not None
            and datos.router_id != servicio.router_id
        ):
            raise ValueError(
                "El cambio de router requiere un flujo de migración técnica"
            )

        for campo in (
            "router_id",
            "plan_id",
            "plantilla_id",
            "zona_id",
            "red_id",
            "olt_id",
            "tecnico_id",
            "mac_address",
        ):
            valor = getattr(datos, campo)
            if valor is not None:
                setattr(servicio, campo, valor)

        # Los servicios creados antes de la migración de multi-domicilio
        # pueden no tener plantilla propia, aunque el cliente sí la tenga.
        if servicio.plantilla_id is None and servicio.cliente.plantilla_id:
            servicio.plantilla_id = servicio.cliente.plantilla_id
        if servicio.plan_id is None and servicio.cliente.plan_id:
            servicio.plan_id = servicio.cliente.plan_id

        await self._validar_catalogos(
            router_id=servicio.router_id,
            plan_id=servicio.plan_id,
            plantilla_id=servicio.plantilla_id,
            red_id=servicio.red_id,
        )
        await self._validar_topologia_fibra(
            router_id=servicio.router_id,
            zona_id=servicio.zona_id,
            olt_id=servicio.olt_id,
            caja_nap_id=datos.caja_nap_id,
        )
        if not servicio.router_id or not servicio.plan_id:
            raise ValueError("El servicio necesita router y plan")
        if (
            servicio.latitud is None
            or servicio.longitud is None
            or (servicio.latitud == 0 and servicio.longitud == 0)
        ):
            raise ValueError(
                "El domicilio necesita coordenadas GPS válidas"
            )

        usuario_pppoe = datos.user_pppoe.strip()
        existente_pppoe = (
            await self.db.execute(
                select(ServicioModel.id).where(
                    ServicioModel.user_pppoe == usuario_pppoe,
                    ServicioModel.id != servicio.id,
                    ServicioModel.estado != "cancelado",
                )
            )
        ).scalar_one_or_none()
        if existente_pppoe:
            raise ValueError("El usuario PPPoE ya pertenece a otro servicio")

        ip_solicitada = (
            datos.ip_asignada.strip()
            if datos.ip_asignada
            else None
        )
        if servicio.red_id:
            servicio.ip_asignada = await IPAMService(
                self.db
            ).reservar_para_servicio(
                red_id=servicio.red_id,
                ip_solicitada=ip_solicitada,
                router_id=servicio.router_id,
                excluir_servicio_id=servicio.id,
            )
        elif ip_solicitada:
            ocupante = (
                await self.db.execute(
                    select(ServicioModel.id).where(
                        ServicioModel.ip_asignada == ip_solicitada,
                        ServicioModel.id != servicio.id,
                        ServicioModel.estado != "cancelado",
                    )
                )
            ).scalar_one_or_none()
            if ocupante:
                raise ValueError("La IP ya pertenece a otro servicio")
            servicio.ip_asignada = ip_solicitada
        else:
            raise ValueError("El servicio necesita una IP asignada")

        servicio.user_pppoe = usuario_pppoe
        servicio.pass_pppoe = datos.pass_pppoe
        await self.db.flush()

        orden = (
            await self.db.execute(
                select(OrdenServicioModel)
                .where(
                    OrdenServicioModel.servicio_id == servicio.id,
                    OrdenServicioModel.tipo == "instalacion",
                    OrdenServicioModel.estado.notin_(
                        ["terminada", "cancelada"]
                    ),
                )
                .order_by(OrdenServicioModel.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        orden_id = orden.id if orden else None
        ftth = FTTHService(self.db)
        if datos.onu_id:
            await ftth.asignar_onu_servicio(
                servicio,
                datos.onu_id,
                usuario_id,
                orden_id=orden_id,
            )
        if bool(datos.caja_nap_id) != bool(datos.puerto_nap):
            raise ValueError("Debes indicar caja NAP y puerto")
        if datos.caja_nap_id and datos.puerto_nap:
            await ftth.asignar_puerto_servicio(
                servicio,
                datos.caja_nap_id,
                datos.puerto_nap,
                usuario_id,
                orden_id=orden_id,
            )

        fecha_instalacion = datos.fecha_instalacion or date.today()
        fecha_activacion = datos.fecha_activacion or fecha_instalacion
        fechas = BillingCalendarService.calcular_fechas_servicio(
            fecha_instalacion=fecha_instalacion,
            fecha_activacion=fecha_activacion,
            meses_gratis=datos.meses_gratis,
            ciclo_facturacion=datos.ciclo_facturacion.value,
        )
        servicio.tipo_facturacion = TipoFacturacion(
            datos.tipo_facturacion.value
        )
        servicio.ciclo_facturacion = CicloFacturacion(
            datos.ciclo_facturacion.value
        )
        servicio.fecha_instalacion = fechas.fecha_instalacion
        servicio.fecha_activacion = fechas.fecha_activacion
        servicio.fecha_inicio_servicio = fechas.fecha_inicio_servicio
        servicio.fecha_fin_periodo_gratis = (
            fechas.fecha_fin_periodo_gratis
        )
        servicio.fecha_inicio_cobro = fechas.fecha_inicio_cobro
        servicio.proxima_facturacion = fechas.proxima_facturacion
        servicio.meses_gratis = datos.meses_gratis

        plantilla = (
            await self.db.get(
                PlantillaFacturacionModel,
                servicio.plantilla_id,
            )
            if servicio.plantilla_id
            else None
        )
        servicio.dia_vencimiento = (
            plantilla.dia_pago if plantilla else None
        )
        servicio.dias_tolerancia = (
            (plantilla.dias_tolerancia or 0)
            if plantilla
            else 0
        )

        router = await self.db.get(RouterModel, servicio.router_id)
        plan = await self.db.get(PlanModel, servicio.plan_id)
        mk = MikroTikService(
            router.ip_vpn,
            router.user_api,
            router.pass_api,
            router.port_api,
        )
        mk.crear_actualizar_pppoe(
            user=servicio.user_pppoe,
            password=servicio.pass_pppoe,
            profile=plan.nombre,
            remote_address=servicio.ip_asignada,
            comment=(
                f"{servicio.cliente.nombre} | "
                f"Servicio:{servicio.id} {servicio.alias}"
            ),
        )

        servicio.estado = "activo"
        servicio.is_online = False
        servicio.ultimo_cambio_estado = datetime.now()
        if orden:
            orden.estado = "terminada"
            orden.fecha_finalizacion = datetime.now()
        await self._sincronizar_estado_cliente(servicio.cliente_id)
        await self._sincronizar_legacy_si_principal(servicio)
        await self.db.commit()
        return await self.obtener(servicio.id)

    async def cambiar_plan(
        self,
        servicio_id: int,
        datos: ServicioPlanUpdate,
    ) -> dict:
        """Cambia el plan del contrato y lo aplica al MikroTik correspondiente."""
        servicio = await self.obtener(servicio_id)
        if servicio.estado == "cancelado":
            raise ValueError("No se puede cambiar el plan de un servicio cancelado")
        if not servicio.router_id:
            raise ValueError("El servicio no tiene un router asignado")

        plan = await self.db.get(PlanModel, datos.plan_id)
        if not plan:
            raise ValueError("Plan no encontrado")
        if plan.router_id != servicio.router_id:
            raise ValueError(
                "El plan seleccionado no pertenece al MikroTik de este servicio"
            )

        if servicio.plan_id == plan.id:
            return {
                "servicio": servicio,
                "mikrotik_sincronizado": None,
                "mensaje": "El servicio ya tiene asignado ese plan",
            }

        plan_anterior = servicio.plan.nombre if servicio.plan else "Sin plan"
        servicio.plan_id = plan.id
        servicio.plan = plan
        await self._sincronizar_legacy_si_principal(servicio)

        # La base de datos es el estado deseado. Se confirma primero para que
        # facturación use el precio nuevo y el conciliador pueda reintentar si
        # el router está temporalmente fuera de línea.
        await self.db.commit()

        if servicio.estado not in {"activo", "suspendido"}:
            return {
                "servicio": servicio,
                "mikrotik_sincronizado": None,
                "mensaje": (
                    f"Plan cambiado de {plan_anterior} a {plan.nombre}; "
                    "se aplicará en MikroTik al activar la instalación"
                ),
            }

        try:
            await self._aplicar_plan_en_mikrotik(servicio)
            return {
                "servicio": servicio,
                "mikrotik_sincronizado": True,
                "mensaje": (
                    f"Plan cambiado de {plan_anterior} a {plan.nombre} "
                    "y confirmado en MikroTik"
                ),
            }
        except Exception as exc:
            self.db.add(
                LogCronjobModel(
                    nivel="ERROR",
                    origen="ConciliacionMikroTik",
                    mensaje=(
                        f"Cambio de plan guardado para servicio={servicio.id} "
                        f"cliente={servicio.cliente_id} "
                        f"pppoe='{servicio.user_pppoe}': "
                        f"{plan_anterior} -> {plan.nombre}; "
                        f"MikroTik no lo confirmó y se reintentará: {exc}"
                    ),
                )
            )
            await self.db.commit()
            return {
                "servicio": servicio,
                "mikrotik_sincronizado": False,
                "mensaje": (
                    "El plan y el nuevo precio quedaron guardados, pero "
                    "MikroTik no respondió. La conciliación automática "
                    "volverá a intentarlo."
                ),
            }

    async def _aplicar_plan_en_mikrotik(
        self,
        servicio: ServicioModel,
    ):
        if (
            not servicio.router
            or not servicio.plan
            or not servicio.user_pppoe
            or not servicio.pass_pppoe
            or not servicio.ip_asignada
        ):
            raise ValueError(
                "El servicio no tiene completa su configuración MikroTik"
            )

        mk = MikroTikService(
            servicio.router.ip_vpn,
            servicio.router.user_api,
            servicio.router.pass_api,
            servicio.router.port_api,
        )
        await asyncio.to_thread(
            mk.crear_actualizar_pppoe,
            servicio.user_pppoe,
            servicio.pass_pppoe,
            servicio.plan.nombre,
            servicio.ip_asignada,
            (
                f"{servicio.cliente.nombre} | "
                f"Servicio:{servicio.id} {servicio.alias}"
            ),
        )

        debe_suspender = servicio.estado == "suspendido"
        encontrado = await asyncio.to_thread(
            mk.activar_desactivar_pppoe,
            servicio.user_pppoe,
            debe_suspender,
        )
        if encontrado is not True:
            raise RuntimeError("MikroTik no encontró el usuario PPPoE")
        confirmado = await asyncio.to_thread(
            mk.gestionar_corte_cliente,
            servicio.ip_asignada,
            debe_suspender,
        )
        if confirmado is not True:
            raise RuntimeError("MikroTik no confirmó el estado de corte")

        secret = await asyncio.to_thread(
            mk.obtener_pppoe_estricto,
            servicio.user_pppoe,
        )
        if not secret or str(secret.get("profile", "")).strip() != (
            servicio.plan.nombre.strip()
        ):
            raise RuntimeError("MikroTik no confirmó el perfil del plan")

    async def cambiar_estado(
        self,
        servicio_id: int,
        estado: str,
    ) -> ServicioModel:
        if estado not in {"activo", "suspendido"}:
            raise ValueError("Usa únicamente activo o suspendido")
        servicio = await self.obtener(servicio_id)
        if servicio.estado == "cancelado":
            raise ValueError("El servicio está cancelado")
        if not servicio.router or not servicio.user_pppoe:
            raise ValueError("El servicio no tiene configuración MikroTik")

        mk = MikroTikService(
            servicio.router.ip_vpn,
            servicio.router.user_api,
            servicio.router.pass_api,
            servicio.router.port_api,
        )
        suspendido = estado == "suspendido"
        encontrado = await asyncio.to_thread(
            mk.activar_desactivar_pppoe,
            servicio.user_pppoe,
            suspendido,
        )
        if encontrado is False:
            raise ValueError("MikroTik no encontró el usuario PPPoE")
        if servicio.ip_asignada:
            confirmado = await asyncio.to_thread(
                mk.gestionar_corte_cliente,
                servicio.ip_asignada,
                suspendido,
            )
            if confirmado is not True:
                raise RuntimeError("MikroTik no confirmó el estado de corte")

        servicio.estado = estado
        servicio.is_online = False if estado == "suspendido" else servicio.is_online
        # Evita que las consultas de sincronización disparen un autoflush
        # implícito fuera del contexto async de SQLAlchemy.
        await self.db.flush()
        await self._sincronizar_estado_cliente(servicio.cliente_id)
        await self.db.flush()
        await self._sincronizar_legacy_si_principal(servicio)
        await self.db.flush()
        await self.db.commit()
        return await self.obtener(servicio.id)

    async def _validar_catalogos(
        self,
        *,
        router_id: int | None,
        plan_id: int | None,
        plantilla_id: int | None,
        red_id: int | None,
    ):
        router = await self.db.get(RouterModel, router_id) if router_id else None
        if router_id and not router:
            raise ValueError("Router no encontrado")
        plan = await self.db.get(PlanModel, plan_id) if plan_id else None
        if plan_id and not plan:
            raise ValueError("Plan no encontrado")
        if plan and router_id and plan.router_id != router_id:
            raise ValueError("El plan no pertenece al router seleccionado")
        if plantilla_id and not await self.db.get(
            PlantillaFacturacionModel,
            plantilla_id,
        ):
            raise ValueError("Plantilla de facturación no encontrada")
        red = await self.db.get(RedModel, red_id) if red_id else None
        if red_id and not red:
            raise ValueError("Red no encontrada")
        if red and router_id and red.router_id != router_id:
            raise ValueError("La red no pertenece al router seleccionado")

    async def _validar_topologia_fibra(
        self,
        *,
        router_id: int | None,
        zona_id: int | None,
        olt_id: int | None,
        caja_nap_id: int | None,
    ):
        olt = await self.db.get(OLTModel, olt_id) if olt_id else None
        if olt_id and not olt:
            raise ValueError("OLT no encontrada")
        if olt and router_id and olt.router_id != router_id:
            raise ValueError("La OLT no pertenece al router seleccionado")

        caja = (
            await self.db.get(CajaNapModel, caja_nap_id)
            if caja_nap_id
            else None
        )
        if caja_nap_id and not caja:
            raise ValueError("Caja NAP no encontrada")
        if not caja:
            return
        if zona_id and caja.zona_id != zona_id:
            raise ValueError("La caja NAP no pertenece a la zona seleccionada")
        if olt_id and caja.olt_id and caja.olt_id != olt_id:
            raise ValueError("La caja NAP no pertenece a la OLT seleccionada")
        if caja.olt_id and router_id:
            olt_caja = await self.db.get(OLTModel, caja.olt_id)
            if olt_caja and olt_caja.router_id != router_id:
                raise ValueError(
                    "La caja NAP no pertenece al router seleccionado"
                )

    async def _sincronizar_estado_cliente(self, cliente_id: int):
        cliente = await self.db.get(ClienteModel, cliente_id)
        if not cliente:
            return
        estados = (
            await self.db.execute(
                select(ServicioModel.estado).where(
                    ServicioModel.cliente_id == cliente_id,
                    ServicioModel.estado != "cancelado",
                )
            )
        ).scalars().all()
        if "activo" in estados:
            cliente.estado = "activo"
        elif "suspendido" in estados:
            cliente.estado = "suspendido"
        elif "pendiente_instalacion" in estados:
            cliente.estado = "pendiente_instalacion"
        else:
            cliente.estado = "cancelado"

    async def _sincronizar_legacy_si_principal(
        self,
        servicio: ServicioModel,
    ):
        principal_id = (
            await self.db.execute(
                select(func.min(ServicioModel.id)).where(
                    ServicioModel.cliente_id == servicio.cliente_id,
                    ServicioModel.estado != "cancelado",
                )
            )
        ).scalar_one()
        if principal_id != servicio.id:
            return
        cliente = await self.db.get(ClienteModel, servicio.cliente_id)
        estado_servicio = instance_state(servicio)
        for campo in (
            "direccion",
            "latitud",
            "longitud",
            "router_id",
            "plan_id",
            "plantilla_id",
            "zona_id",
            "red_id",
            "olt_id",
            "caja_nap_id",
            "puerto_nap",
            "tecnico_id",
            "onu_id",
            "ip_asignada",
            "mac_address",
            "user_pppoe",
            "pass_pppoe",
            "is_online",
            "ultimo_cambio_estado",
        ):
            valor = estado_servicio.dict.get(campo, NO_VALUE)
            if valor is not NO_VALUE:
                setattr(cliente, campo, valor)
        proxima_facturacion = estado_servicio.dict.get(
            "proxima_facturacion",
            NO_VALUE,
        )
        if proxima_facturacion is not NO_VALUE:
            cliente.proxima_factura = proxima_facturacion
