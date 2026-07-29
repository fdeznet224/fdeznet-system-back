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

        existente = await self._baja_abierta(cliente.id)
        if existente:
            return existente
        if cliente.estado in {"cancelado", "eliminado"}:
            raise ValueError("El cliente ya se encuentra cancelado")

        tecnico = await self._validar_tecnico(tecnico_id)
        servicio = (
            await self.db.execute(
                select(ServicioModel)
                .where(
                    ServicioModel.cliente_id == cliente.id,
                    ServicioModel.estado != "cancelado",
                )
                .order_by(ServicioModel.id.desc())
                .with_for_update()
            )
        ).scalars().first()
        caja_nap_id = cliente.caja_nap_id
        puerto_nap = cliente.puerto_nap
        onu = None
        if cliente.onu_id:
            onu = (
                await self.db.execute(
                    select(InventarioONUModel)
                    .where(InventarioONUModel.id == cliente.onu_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()

        await self._cancelar_ordenes_abiertas(cliente.id, usuario.id)

        orden = None
        if onu:
            estado_orden = "asignada" if tecnico else "pendiente"
            orden = (
                await self.db.execute(
                    select(OrdenServicioModel)
                    .where(
                        OrdenServicioModel.cliente_id == cliente.id,
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
            ip_snapshot=cliente.ip_asignada,
            caja_nap_id_snapshot=caja_nap_id,
            puerto_nap_snapshot=puerto_nap,
            servicio_estado_snapshot=servicio.estado if servicio else None,
            proxima_facturacion_snapshot=(
                servicio.proxima_facturacion if servicio else None
            ),
        )
        self.db.add(baja)

        cliente.estado = "cancelado"
        cliente.proxima_factura = None
        if servicio:
            servicio.estado = "cancelado"
            servicio.proxima_facturacion = None
        await FTTHService(self.db).liberar_puerto(cliente, usuario.id)
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

        if cliente and cliente.onu_id == onu.id:
            cliente.onu_id = None
            cliente.mac_address = None
            cliente.ip_asignada = None

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
        if not cliente or not onu or cliente.onu_id != onu.id:
            raise ValueError(
                "El equipo ya fue desvinculado; crea una nueva instalación"
            )
        if onu.estado != "POR_RECOGER":
            raise ValueError("La ONU ya no está disponible para reactivación")
        if not servicio:
            raise ValueError("No existe el servicio original para reactivar")

        if baja.caja_nap_id_snapshot and baja.puerto_nap_snapshot:
            await FTTHService(self.db).asignar_puerto(
                cliente=cliente,
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
            tecnico_id=usuario.id,
            orden_id=baja.orden_retiro_id,
            tipo_movimiento="reactivacion_baja",
            estado_anterior=estado_anterior,
            estado_nuevo="INSTALADO",
            motivo="Baja revertida antes de recuperar el equipo",
        )

        cliente.estado = "activo"
        cliente.ip_asignada = baja.ip_snapshot
        cliente.proxima_factura = baja.proxima_facturacion_snapshot
        servicio.estado = "activo"
        servicio.proxima_facturacion = baja.proxima_facturacion_snapshot
        baja.estado = "cancelada"
        baja.cancelada_en = datetime.now()
        await self._cancelar_orden_retiro(baja, usuario.id)
        await self.db.commit()

        await self.sincronizar_mikrotik(baja.id)
        return await self.obtener(baja.id, usuario)

    async def sincronizar_mikrotik(self, baja_id: int):
        baja = await self.obtener(baja_id)
        cliente = baja.cliente
        activar = baja.estado == "cancelada"
        if not cliente.router or (
            not cliente.user_pppoe and not cliente.ip_asignada
        ):
            baja.mikrotik_estado = "no_aplica"
            baja.mikrotik_error = None
            await self.db.commit()
            return baja

        try:
            mk = MikroTikService(
                cliente.router.ip_vpn,
                cliente.router.user_api,
                cliente.router.pass_api,
                cliente.router.port_api,
            )
            if activar and cliente.user_pppoe:
                if not cliente.plan or not cliente.pass_pppoe:
                    raise ValueError(
                        "Faltan plan o credenciales PPPoE para reactivar"
                    )
                mk.crear_actualizar_pppoe(
                    user=cliente.user_pppoe,
                    password=cliente.pass_pppoe,
                    profile=cliente.plan.nombre,
                    remote_address=cliente.ip_asignada,
                    comment=(
                        f"{cliente.nombre} | ID:{cliente.cedula or 'S/A'}"
                    ),
                )
                baja.mikrotik_estado = "reactivado"
            elif activar:
                mk.gestionar_corte_cliente(
                    cliente.ip_asignada,
                    suspender=False,
                )
                baja.mikrotik_estado = "reactivado"
            elif cliente.user_pppoe:
                encontrado = mk.activar_desactivar_pppoe(
                    cliente.user_pppoe,
                    disabled=True,
                )
                baja.mikrotik_estado = (
                    "deshabilitado" if encontrado else "no_encontrado"
                )
            else:
                mk.gestionar_corte_cliente(
                    cliente.ip_asignada,
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

    async def _baja_abierta(self, cliente_id: int):
        return (
            await self.db.execute(
                select(BajaServicioModel)
                .where(
                    BajaServicioModel.cliente_id == cliente_id,
                    BajaServicioModel.estado.in_(ESTADOS_BAJA_ABIERTA),
                )
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

    async def _cancelar_ordenes_abiertas(
        self,
        cliente_id: int,
        usuario_id: int,
    ):
        ordenes = (
            await self.db.execute(
                select(OrdenServicioModel)
                .where(
                    OrdenServicioModel.cliente_id == cliente_id,
                    OrdenServicioModel.tipo != "retiro",
                    OrdenServicioModel.estado.notin_(
                        ["terminada", "cancelada"]
                    ),
                )
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
            selectinload(BajaServicioModel.onu),
            selectinload(BajaServicioModel.orden_retiro),
            selectinload(BajaServicioModel.tecnico),
            selectinload(BajaServicioModel.solicitada_por),
        )
