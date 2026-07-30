from datetime import date, datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.application.services.ftth_service import FTTHService
from src.infrastructure.mikrotik_service import MikroTikService
from src.infrastructure.models import (
    BajaServicioModel,
    ClienteModel,
    HistorialEstadoOrdenModel,
    InventarioONUModel,
    OrdenServicioModel,
    PlanModel,
    RouterModel,
    ServicioModel,
    UsuarioModel,
)


ESTADOS_BAJA_ABIERTA = {"pendiente_retiro", "sin_equipo"}
CONDICIONES_EQUIPO = {"funcional", "danada", "incompleta", "perdida"}
ESTADO_INVENTARIO_POR_CONDICION = {
    "funcional": "DISPONIBLE",
    "danada": "DANADA",
    "incompleta": "INCOMPLETA",
    "perdida": "PERDIDA",
}


class BajaService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def crear(
        self,
        cliente_id: int,
        motivo: str,
        usuario: UsuarioModel,
        tecnico_id: Optional[int] = None,
        fecha_programada: Optional[datetime] = None,
        observaciones: Optional[str] = None,
        servicio_id: Optional[int] = None,
    ) -> BajaServicioModel:
        motivo = (motivo or "").strip()
        if len(motivo) < 5:
            raise ValueError("Indica el motivo de la baja")

        cliente = (
            await self.db.execute(
                select(ClienteModel)
                .options(selectinload(ClienteModel.router))
                .where(ClienteModel.id == cliente_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if not cliente:
            raise ValueError("Cliente no encontrado")

        tecnico = await self._validar_tecnico(tecnico_id)
        servicios = (
            await self.db.execute(
                select(ServicioModel)
                .where(
                    ServicioModel.cliente_id == cliente.id,
                    ServicioModel.estado != "cancelado",
                )
                .order_by(ServicioModel.id)
                .with_for_update()
            )
        ).scalars().all()
        if servicio_id:
            servicio = next(
                (item for item in servicios if item.id == servicio_id),
                None,
            )
            if not servicio:
                raise ValueError("El servicio no pertenece al cliente")
        elif len(servicios) == 1:
            servicio = servicios[0]
        elif len(servicios) > 1:
            raise ValueError(
                "Indica servicio_id porque el cliente tiene "
                "varios domicilios"
            )
        else:
            raise ValueError("El cliente no tiene servicios vigentes")

        existente = await self._baja_abierta(
            cliente.id,
            servicio.id,
        )
        if existente:
            return existente

        caja_nap_id = servicio.caja_nap_id
        puerto_nap = servicio.puerto_nap
        onu = None
        if servicio.onu_id:
            onu = (
                await self.db.execute(
                    select(InventarioONUModel)
                    .where(InventarioONUModel.id == servicio.onu_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()

        await self._cancelar_ordenes_abiertas(
            cliente.id,
            usuario.id,
            servicio.id,
        )

        orden = None
        if onu:
            estado_orden = "asignada" if tecnico else "pendiente"
            orden = (
                await self.db.execute(
                    select(OrdenServicioModel)
                    .where(
                        OrdenServicioModel.cliente_id == cliente.id,
                        OrdenServicioModel.servicio_id == servicio.id,
                        OrdenServicioModel.tipo == "retiro",
                        OrdenServicioModel.estado.notin_(
                            ["terminada", "cancelada"]
                        ),
                    )
                    .order_by(OrdenServicioModel.id.desc())
                    .with_for_update()
                )
            ).scalars().first()
            if orden:
                anterior = orden.estado
                orden.tecnico_id = tecnico.id if tecnico else orden.tecnico_id
                orden.fecha_programada = fecha_programada or orden.fecha_programada
                orden.prioridad = "alta"
                orden.motivo = "Recuperación de ONU por baja"
                orden.descripcion = (
                    f"Recoger ONU {onu.identificador}. Motivo: {motivo}"
                )
                if tecnico and orden.estado == "pendiente":
                    orden.estado = "asignada"
                orden.version += 1
                self.db.add(
                    HistorialEstadoOrdenModel(
                        orden_id=orden.id,
                        usuario_id=usuario.id,
                        estado_anterior=anterior,
                        estado_nuevo=orden.estado,
                        comentario="Orden vinculada al expediente de baja",
                    )
                )
            else:
                orden = OrdenServicioModel(
                    tipo="retiro",
                    cliente_id=cliente.id,
                    servicio_id=servicio.id,
                    tecnico_id=tecnico.id if tecnico else None,
                    creado_por_id=usuario.id,
                    prioridad="alta",
                    estado=estado_orden,
                    fecha_programada=fecha_programada,
                    motivo="Recuperación de ONU por baja",
                    descripcion=(
                        f"Recoger ONU {onu.identificador}. Motivo: {motivo}"
                    ),
                )
                self.db.add(orden)
                await self.db.flush()
                self.db.add(
                    HistorialEstadoOrdenModel(
                        orden_id=orden.id,
                        usuario_id=usuario.id,
                        estado_anterior=None,
                        estado_nuevo=estado_orden,
                        comentario=(
                            "Creada automáticamente por baja de servicio"
                        ),
                    )
                )

            estado_anterior = onu.estado
            onu.estado = "POR_RECOGER"
            onu.tecnico_id = tecnico.id if tecnico else None
            FTTHService(self.db).registrar_movimiento(
                onu=onu,
                cliente_id=cliente.id,
                servicio_id=servicio.id,
                tecnico_id=usuario.id,
                orden_id=orden.id,
                tipo_movimiento="retiro_pendiente",
                estado_anterior=estado_anterior,
                estado_nuevo="POR_RECOGER",
                motivo=motivo,
            )

        baja = BajaServicioModel(
            cliente_id=cliente.id,
            servicio_id=servicio.id if servicio else None,
            orden_retiro_id=orden.id if orden else None,
            onu_id=onu.id if onu else None,
            solicitada_por_id=usuario.id,
            tecnico_id=tecnico.id if tecnico else None,
            estado="pendiente_retiro" if onu else "sin_equipo",
            motivo=motivo,
            observaciones=(observaciones or "").strip() or None,
            mikrotik_estado="pendiente",
            ip_snapshot=servicio.ip_asignada,
            caja_nap_id_snapshot=caja_nap_id,
            puerto_nap_snapshot=puerto_nap,
            servicio_estado_snapshot=servicio.estado if servicio else None,
            proxima_facturacion_snapshot=(
                servicio.proxima_facturacion if servicio else None
            ),
        )
        self.db.add(baja)

        servicio.estado = "cancelado"
        servicio.proxima_facturacion = None
        await FTTHService(self.db).liberar_puerto_servicio(
            servicio,
            usuario.id,
        )
        await self._sincronizar_estado_cliente(cliente)
        await self._sincronizar_legacy_si_principal(servicio, cliente)
        await self.db.commit()

        await self.sincronizar_mikrotik(baja.id)
        return await self.obtener(baja.id, usuario)

    async def listar(
        self,
        usuario: UsuarioModel,
        estado: Optional[str] = None,
        tecnico_id: Optional[int] = None,
        cliente_id: Optional[int] = None,
        limite: int = 100,
    ):
        stmt = self._consulta_base()
        if usuario.rol == "tecnico":
            stmt = stmt.where(BajaServicioModel.tecnico_id == usuario.id)
        elif tecnico_id:
            stmt = stmt.where(BajaServicioModel.tecnico_id == tecnico_id)
        if estado:
            stmt = stmt.where(BajaServicioModel.estado == estado)
        if cliente_id:
            stmt = stmt.where(BajaServicioModel.cliente_id == cliente_id)
        return (
            await self.db.execute(
                stmt.order_by(BajaServicioModel.id.desc()).limit(limite)
            )
        ).scalars().unique().all()

    async def obtener(
        self,
        baja_id: int,
        usuario: Optional[UsuarioModel] = None,
    ) -> BajaServicioModel:
        baja = (
            await self.db.execute(
                self._consulta_base().where(BajaServicioModel.id == baja_id)
            )
        ).scalar_one_or_none()
        if not baja:
            raise ValueError("Expediente de baja no encontrado")
        if (
            usuario
            and usuario.rol == "tecnico"
            and baja.tecnico_id != usuario.id
        ):
            raise PermissionError("La baja no está asignada a este técnico")
        return baja

    async def asignar_tecnico(
        self,
        baja_id: int,
        tecnico_id: int,
        usuario: UsuarioModel,
    ):
        baja = await self._obtener_bloqueada(baja_id)
        if baja.estado != "pendiente_retiro":
            raise ValueError("La baja ya no tiene un retiro pendiente")
        tecnico = await self._validar_tecnico(tecnico_id)
        baja.tecnico_id = tecnico.id
        if baja.onu_id:
            onu = await self.db.get(InventarioONUModel, baja.onu_id)
            if onu:
                onu.tecnico_id = tecnico.id
        if baja.orden_retiro_id:
            orden = await self.db.get(
                OrdenServicioModel,
                baja.orden_retiro_id,
            )
            if orden:
                anterior = orden.estado
                orden.tecnico_id = tecnico.id
                if orden.estado == "pendiente":
                    orden.estado = "asignada"
                orden.version += 1
                self.db.add(
                    HistorialEstadoOrdenModel(
                        orden_id=orden.id,
                        usuario_id=usuario.id,
                        estado_anterior=anterior,
                        estado_nuevo=orden.estado,
                        comentario=(
                            f"Retiro asignado al técnico {tecnico.id}"
                        ),
                    )
                )
        await self.db.commit()
        return await self.obtener(baja.id, usuario)

    async def confirmar_retiro(
        self,
        baja_id: int,
        condicion: str,
        usuario: UsuarioModel,
        observaciones: Optional[str] = None,
    ):
        condicion = condicion.strip().lower()
        estado_inventario = self.estado_inventario(condicion)
        baja = await self._obtener_bloqueada(baja_id)
        if baja.estado != "pendiente_retiro":
            raise ValueError("La baja ya no tiene un retiro pendiente")
        if (
            usuario.rol == "tecnico"
            and baja.tecnico_id != usuario.id
        ):
            raise PermissionError("El retiro no está asignado a este técnico")
        if usuario.rol == "tecnico" and baja.orden_retiro_id:
            orden = await self.db.get(
                OrdenServicioModel,
                baja.orden_retiro_id,
            )
            if not orden or orden.estado != "trabajando":
                raise ValueError(
                    "La orden debe estar en estado trabajando para cerrar el retiro"
                )
        if not baja.onu_id:
            raise ValueError("La baja no tiene una ONU vinculada")

        onu = (
            await self.db.execute(
                select(InventarioONUModel)
                .where(InventarioONUModel.id == baja.onu_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if not onu:
            raise ValueError("La ONU ya no existe en inventario")
        if onu.estado != "POR_RECOGER":
            raise ValueError("La ONU no está marcada para recolección")

        cliente = await self.db.get(ClienteModel, baja.cliente_id)
        estado_anterior = onu.estado
        onu.estado = estado_inventario
        onu.tecnico_id = None
        FTTHService(self.db).registrar_movimiento(
            onu=onu,
            cliente_id=baja.cliente_id,
            servicio_id=baja.servicio_id,
            tecnico_id=usuario.id,
            orden_id=baja.orden_retiro_id,
            tipo_movimiento=(
                "equipo_no_recuperado"
                if condicion == "perdida"
                else "ingreso_bodega"
            ),
            estado_anterior=estado_anterior,
            estado_nuevo=estado_inventario,
            condicion=condicion.upper(),
            motivo=(observaciones or "Cierre de retiro por baja"),
        )

        servicio = (
            await self.db.get(ServicioModel, baja.servicio_id)
            if baja.servicio_id
            else None
        )
        if servicio and servicio.onu_id == onu.id:
            servicio.onu_id = None
            servicio.mac_address = None
        if cliente and cliente.onu_id == onu.id:
            cliente.onu_id = None
            cliente.mac_address = None

        await self._cerrar_orden_retiro(
            baja,
            usuario.id,
            onu.identificador,
            condicion,
        )
        baja.estado = (
            "cerrada_no_recuperada"
            if condicion == "perdida"
            else "recuperada"
        )
        baja.condicion_equipo = condicion
        baja.recuperada_en = datetime.now()
        if observaciones:
            baja.observaciones = observaciones.strip()
        await self.db.commit()
        return await self.obtener(baja.id, usuario)

    async def cancelar_y_reactivar(
        self,
        baja_id: int,
        usuario: UsuarioModel,
    ):
        baja = await self._obtener_bloqueada(baja_id)
        if baja.estado != "pendiente_retiro":
            raise ValueError(
                "Solo se puede revertir una baja antes de recuperar la ONU"
            )
        if baja.solicitada_por_id is None:
            raise ValueError(
                "Esta baja es anterior al expediente formal; crea una nueva "
                "instalación para reactivar con un puerto validado"
            )
        cliente = await self.db.get(ClienteModel, baja.cliente_id)
        onu = await self.db.get(InventarioONUModel, baja.onu_id)
        servicio = (
            await self.db.get(ServicioModel, baja.servicio_id)
            if baja.servicio_id
            else None
        )
        if not cliente or not onu:
            raise ValueError(
                "El equipo ya fue desvinculado; crea una nueva instalación"
            )
        if onu.estado != "POR_RECOGER":
            raise ValueError("La ONU ya no está disponible para reactivación")
        if not servicio:
            raise ValueError("No existe el servicio original para reactivar")
        if servicio.onu_id != onu.id:
            raise ValueError(
                "El equipo ya fue desvinculado; crea una nueva instalación"
            )

        if baja.caja_nap_id_snapshot and baja.puerto_nap_snapshot:
            await FTTHService(self.db).asignar_puerto_servicio(
                servicio=servicio,
                caja_nap_id=baja.caja_nap_id_snapshot,
                numero=baja.puerto_nap_snapshot,
                usuario_id=usuario.id,
            )
        estado_anterior = onu.estado
        onu.estado = "INSTALADO"
        onu.tecnico_id = None
        FTTHService(self.db).registrar_movimiento(
            onu=onu,
            cliente_id=cliente.id,
            servicio_id=servicio.id,
            tecnico_id=usuario.id,
            orden_id=baja.orden_retiro_id,
            tipo_movimiento="reactivacion_baja",
            estado_anterior=estado_anterior,
            estado_nuevo="INSTALADO",
            motivo="Baja revertida antes de recuperar el equipo",
        )

        servicio.estado = "activo"
        servicio.ip_asignada = baja.ip_snapshot
        servicio.proxima_facturacion = baja.proxima_facturacion_snapshot
        baja.estado = "cancelada"
        baja.cancelada_en = datetime.now()
        await self._cancelar_orden_retiro(baja, usuario.id)
        await self._sincronizar_estado_cliente(cliente)
        await self._sincronizar_legacy_si_principal(servicio, cliente)
        await self.db.commit()

        await self.sincronizar_mikrotik(baja.id)
        return await self.obtener(baja.id, usuario)

    async def sincronizar_mikrotik(self, baja_id: int):
        baja = await self.obtener(baja_id)
        cliente = baja.cliente
        servicio = baja.servicio
        objetivo = servicio or cliente
        activar = baja.estado == "cancelada"
        router = (
            await self.db.get(RouterModel, objetivo.router_id)
            if objetivo.router_id
            else None
        )
        if not router or (
            not objetivo.user_pppoe and not objetivo.ip_asignada
        ):
            baja.mikrotik_estado = "no_aplica"
            baja.mikrotik_error = None
            await self.db.commit()
            return baja

        try:
            mk = MikroTikService(
                router.ip_vpn,
                router.user_api,
                router.pass_api,
                router.port_api,
            )
            if activar and objetivo.user_pppoe:
                plan = (
                    await self.db.get(PlanModel, objetivo.plan_id)
                    if objetivo.plan_id
                    else None
                )
                if not plan or not objetivo.pass_pppoe:
                    raise ValueError(
                        "Faltan plan o credenciales PPPoE para reactivar"
                    )
                mk.crear_actualizar_pppoe(
                    user=objetivo.user_pppoe,
                    password=objetivo.pass_pppoe,
                    profile=plan.nombre,
                    remote_address=objetivo.ip_asignada,
                    comment=(
                        f"{cliente.nombre} | Servicio:"
                        f"{servicio.id if servicio else 'legacy'}"
                    ),
                )
                baja.mikrotik_estado = "reactivado"
            elif activar:
                mk.gestionar_corte_cliente(
                    objetivo.ip_asignada,
                    suspender=False,
                )
                baja.mikrotik_estado = "reactivado"
            elif objetivo.user_pppoe:
                encontrado = mk.activar_desactivar_pppoe(
                    objetivo.user_pppoe,
                    disabled=True,
                )
                baja.mikrotik_estado = (
                    "deshabilitado" if encontrado else "no_encontrado"
                )
            else:
                mk.gestionar_corte_cliente(
                    objetivo.ip_asignada,
                    suspender=True,
                )
                baja.mikrotik_estado = "deshabilitado"
            baja.mikrotik_error = None
        except Exception as exc:
            baja.mikrotik_estado = "error"
            baja.mikrotik_error = str(exc)[:2000]
        await self.db.commit()
        return baja

    @staticmethod
    def estado_inventario(condicion: str) -> str:
        if condicion not in CONDICIONES_EQUIPO:
            raise ValueError("Condición de equipo inválida")
        return ESTADO_INVENTARIO_POR_CONDICION[condicion]

    async def _baja_abierta(
        self,
        cliente_id: int,
        servicio_id: Optional[int] = None,
    ):
        condiciones = [
            BajaServicioModel.cliente_id == cliente_id,
            BajaServicioModel.estado.in_(ESTADOS_BAJA_ABIERTA),
        ]
        if servicio_id:
            condiciones.append(
                BajaServicioModel.servicio_id == servicio_id
            )
        return (
            await self.db.execute(
                select(BajaServicioModel)
                .where(*condiciones)
                .order_by(BajaServicioModel.id.desc())
            )
        ).scalars().first()

    async def _obtener_bloqueada(self, baja_id: int):
        baja = (
            await self.db.execute(
                select(BajaServicioModel)
                .where(BajaServicioModel.id == baja_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if not baja:
            raise ValueError("Expediente de baja no encontrado")
        return baja

    async def _validar_tecnico(self, tecnico_id: Optional[int]):
        if not tecnico_id:
            return None
        tecnico = await self.db.get(UsuarioModel, tecnico_id)
        if not tecnico or not tecnico.activo or tecnico.rol != "tecnico":
            raise ValueError("El técnico no existe o no está activo")
        return tecnico

    async def _sincronizar_estado_cliente(self, cliente: ClienteModel):
        estados = (
            await self.db.execute(
                select(ServicioModel.estado).where(
                    ServicioModel.cliente_id == cliente.id,
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
        cliente: ClienteModel,
    ):
        principal_id = (
            await self.db.execute(
                select(ServicioModel.id)
                .where(ServicioModel.cliente_id == cliente.id)
                .order_by(ServicioModel.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if principal_id != servicio.id:
            return
        for campo in (
            "caja_nap_id",
            "puerto_nap",
            "onu_id",
            "mac_address",
            "ip_asignada",
        ):
            setattr(cliente, campo, getattr(servicio, campo))
        cliente.proxima_factura = servicio.proxima_facturacion

    async def _cancelar_ordenes_abiertas(
        self,
        cliente_id: int,
        usuario_id: int,
        servicio_id: Optional[int] = None,
    ):
        condiciones = [
            OrdenServicioModel.cliente_id == cliente_id,
            OrdenServicioModel.tipo != "retiro",
            OrdenServicioModel.estado.notin_(
                ["terminada", "cancelada"]
            ),
        ]
        if servicio_id:
            condiciones.append(
                OrdenServicioModel.servicio_id == servicio_id
            )
        ordenes = (
            await self.db.execute(
                select(OrdenServicioModel)
                .where(*condiciones)
                .with_for_update()
            )
        ).scalars().all()
        for orden in ordenes:
            anterior = orden.estado
            orden.estado = "cancelada"
            orden.fecha_cancelacion = datetime.now()
            orden.version += 1
            self.db.add(
                HistorialEstadoOrdenModel(
                    orden_id=orden.id,
                    usuario_id=usuario_id,
                    estado_anterior=anterior,
                    estado_nuevo="cancelada",
                    comentario="Cancelada automáticamente por baja del servicio",
                )
            )

    async def _cerrar_orden_retiro(
        self,
        baja: BajaServicioModel,
        usuario_id: int,
        identificador: str,
        condicion: str,
    ):
        if not baja.orden_retiro_id:
            return
        orden = await self.db.get(
            OrdenServicioModel,
            baja.orden_retiro_id,
        )
        if not orden or orden.estado in {"terminada", "cancelada"}:
            return
        anterior = orden.estado
        orden.estado = "terminada"
        orden.solucion = (
            f"ONU {identificador}: retiro cerrado ({condicion})"
        )
        orden.fecha_finalizacion = datetime.now()
        orden.version += 1
        self.db.add(
            HistorialEstadoOrdenModel(
                orden_id=orden.id,
                usuario_id=usuario_id,
                estado_anterior=anterior,
                estado_nuevo="terminada",
                comentario=f"Equipo reportado como {condicion}",
            )
        )

    async def _cancelar_orden_retiro(
        self,
        baja: BajaServicioModel,
        usuario_id: int,
    ):
        if not baja.orden_retiro_id:
            return
        orden = await self.db.get(
            OrdenServicioModel,
            baja.orden_retiro_id,
        )
        if not orden or orden.estado in {"terminada", "cancelada"}:
            return
        anterior = orden.estado
        orden.estado = "cancelada"
        orden.fecha_cancelacion = datetime.now()
        orden.version += 1
        self.db.add(
            HistorialEstadoOrdenModel(
                orden_id=orden.id,
                usuario_id=usuario_id,
                estado_anterior=anterior,
                estado_nuevo="cancelada",
                comentario="Retiro cancelado por reactivación del servicio",
            )
        )

    @staticmethod
    def _consulta_base():
        return select(BajaServicioModel).options(
            selectinload(BajaServicioModel.cliente).selectinload(
                ClienteModel.router
            ),
            selectinload(BajaServicioModel.cliente).selectinload(
                ClienteModel.plan
            ),
            selectinload(BajaServicioModel.servicio),
            selectinload(BajaServicioModel.onu),
            selectinload(BajaServicioModel.orden_retiro),
            selectinload(BajaServicioModel.tecnico),
            selectinload(BajaServicioModel.solicitada_por),
        )
