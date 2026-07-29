from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.models import (
    CajaNapModel,
    ClienteModel,
    HistorialEquipoModel,
    InventarioONUModel,
    LecturaOpticaModel,
    PuertoNapModel,
)


ESTADOS_PUERTO = {"libre", "ocupado", "danado", "reservado"}


class FTTHService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def sincronizar_puertos_nap(self, caja_nap_id: int):
        caja = await self.db.get(CajaNapModel, caja_nap_id)
        if not caja:
            raise ValueError("La caja NAP no existe")

        existentes = set(
            (
                await self.db.execute(
                    select(PuertoNapModel.numero).where(
                        PuertoNapModel.caja_nap_id == caja.id
                    )
                )
            ).scalars()
        )
        for numero in range(1, (caja.capacidad or 0) + 1):
            if numero not in existentes:
                self.db.add(
                    PuertoNapModel(
                        caja_nap_id=caja.id,
                        numero=numero,
                        estado="libre",
                    )
                )
        await self.db.flush()

    async def listar_puertos(self, caja_nap_id: int):
        await self.sincronizar_puertos_nap(caja_nap_id)
        resultado = await self.db.execute(
            select(PuertoNapModel)
            .where(PuertoNapModel.caja_nap_id == caja_nap_id)
            .order_by(PuertoNapModel.numero)
        )
        return resultado.scalars().all()

    async def cambiar_estado_puerto(
        self,
        caja_nap_id: int,
        numero: int,
        estado: str,
        usuario_id: int,
        observaciones: Optional[str] = None,
        orden_id: Optional[int] = None,
    ):
        estado = estado.strip().lower().replace("ñ", "n")
        if estado not in ESTADOS_PUERTO:
            raise ValueError("Estado de puerto inválido")

        puerto = await self._obtener_puerto_bloqueado(caja_nap_id, numero)
        if estado in {"libre", "danado", "reservado"} and puerto.cliente_id:
            raise ValueError("No se puede cambiar el estado de un puerto ocupado")
        if estado == "ocupado" and not puerto.cliente_id:
            raise ValueError("Un puerto sin cliente no puede marcarse como ocupado")
        if estado == "reservado" and not orden_id:
            raise ValueError("Una reserva debe indicar la orden de servicio")

        puerto.estado = estado
        puerto.orden_id = orden_id if estado == "reservado" else None
        puerto.observaciones = observaciones
        puerto.actualizado_por_id = usuario_id
        await self.db.flush()
        return puerto

    async def asignar_puerto(
        self,
        cliente: ClienteModel,
        caja_nap_id: int,
        numero: int,
        usuario_id: int,
        orden_id: Optional[int] = None,
        potencia_dbm: Optional[Decimal] = None,
    ):
        puerto = await self._obtener_puerto_bloqueado(caja_nap_id, numero)

        if puerto.estado == "danado":
            raise ValueError("El puerto NAP está marcado como dañado")
        if (
            puerto.estado == "reservado"
            and puerto.orden_id
            and puerto.orden_id != orden_id
        ):
            raise ValueError("El puerto NAP está reservado para otra orden")
        if puerto.cliente_id and puerto.cliente_id != cliente.id:
            raise ValueError("El puerto NAP ya está ocupado por otro cliente")

        ocupante = (
            await self.db.execute(
                select(ClienteModel).where(
                    ClienteModel.caja_nap_id == caja_nap_id,
                    ClienteModel.puerto_nap == numero,
                    ClienteModel.id != cliente.id,
                )
            )
        ).scalar_one_or_none()
        if ocupante:
            raise ValueError(
                f"El puerto NAP ya pertenece a {ocupante.nombre} "
                f"(cliente {ocupante.id})"
            )

        puerto_anterior = (
            await self.db.execute(
                select(PuertoNapModel)
                .where(
                    PuertoNapModel.cliente_id == cliente.id,
                    PuertoNapModel.id != puerto.id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if puerto_anterior:
            self._liberar_registro_puerto(puerto_anterior, usuario_id)

        puerto.estado = "ocupado"
        puerto.cliente_id = cliente.id
        puerto.orden_id = orden_id
        puerto.actualizado_por_id = usuario_id
        if potencia_dbm is not None:
            puerto.potencia_instalacion_dbm = potencia_dbm

        cliente.caja_nap_id = caja_nap_id
        cliente.puerto_nap = numero
        await self.db.flush()
        return puerto

    async def liberar_puerto(
        self,
        cliente: ClienteModel,
        usuario_id: int,
    ):
        puerto = (
            await self.db.execute(
                select(PuertoNapModel)
                .where(PuertoNapModel.cliente_id == cliente.id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if puerto:
            self._liberar_registro_puerto(puerto, usuario_id)

        cliente.caja_nap_id = None
        cliente.puerto_nap = None
        await self.db.flush()

    async def asignar_onu(
        self,
        cliente: ClienteModel,
        onu_id: int,
        usuario_id: int,
        orden_id: Optional[int] = None,
        motivo: str = "Asignación a cliente",
        potencia_dbm: Optional[Decimal] = None,
        estado_onu_anterior: str = "DISPONIBLE",
        condicion_onu_anterior: Optional[str] = None,
    ):
        onu_nueva = (
            await self.db.execute(
                select(InventarioONUModel)
                .where(InventarioONUModel.id == onu_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if not onu_nueva:
            raise ValueError("La ONU seleccionada no existe")

        ocupante = (
            await self.db.execute(
                select(ClienteModel).where(
                    ClienteModel.onu_id == onu_id,
                    ClienteModel.id != cliente.id,
                )
            )
        ).scalar_one_or_none()
        if ocupante:
            raise ValueError(
                f"La ONU ya está asignada a {ocupante.nombre} "
                f"(cliente {ocupante.id})"
            )
        if cliente.onu_id != onu_id and onu_nueva.estado != "DISPONIBLE":
            raise ValueError(
                f"La ONU no está disponible (estado {onu_nueva.estado})"
            )

        if cliente.onu_id and cliente.onu_id != onu_id:
            onu_anterior = await self.db.get(InventarioONUModel, cliente.onu_id)
            if onu_anterior:
                estado_anterior = onu_anterior.estado
                onu_anterior.estado = estado_onu_anterior
                onu_anterior.tecnico_id = None
                self.registrar_movimiento(
                    onu=onu_anterior,
                    cliente_id=cliente.id,
                    tecnico_id=usuario_id,
                    orden_id=orden_id,
                    tipo_movimiento="desasignacion",
                    estado_anterior=estado_anterior,
                    estado_nuevo=estado_onu_anterior,
                    condicion=condicion_onu_anterior,
                    motivo="Reemplazada durante asignación",
                )

        estado_anterior = onu_nueva.estado
        onu_nueva.estado = "INSTALADO"
        onu_nueva.tecnico_id = usuario_id
        cliente.onu_id = onu_nueva.id
        cliente.mac_address = onu_nueva.identificador
        self.registrar_movimiento(
            onu=onu_nueva,
            cliente_id=cliente.id,
            tecnico_id=usuario_id,
            orden_id=orden_id,
            tipo_movimiento="instalacion",
            estado_anterior=estado_anterior,
            estado_nuevo="INSTALADO",
            motivo=motivo,
            potencia_dbm=potencia_dbm,
        )
        await self.db.flush()
        return onu_nueva

    def registrar_movimiento(
        self,
        onu: InventarioONUModel,
        tipo_movimiento: str,
        estado_nuevo: str,
        cliente_id: Optional[int] = None,
        tecnico_id: Optional[int] = None,
        orden_id: Optional[int] = None,
        estado_anterior: Optional[str] = None,
        condicion: Optional[str] = None,
        motivo: Optional[str] = None,
        potencia_dbm: Optional[Decimal] = None,
    ):
        self.db.add(
            HistorialEquipoModel(
                onu_id=onu.id,
                cliente_id=cliente_id,
                tecnico_id=tecnico_id,
                orden_id=orden_id,
                tipo_movimiento=tipo_movimiento,
                estado_anterior=estado_anterior,
                estado_nuevo=estado_nuevo,
                condicion=condicion,
                motivo=motivo,
                potencia_optica_dbm=potencia_dbm,
            )
        )

    async def registrar_lectura_optica(
        self,
        cliente: ClienteModel,
        potencia_rx_dbm: Decimal,
        tecnico_id: int,
        potencia_tx_dbm: Optional[Decimal] = None,
        orden_id: Optional[int] = None,
        origen: str = "manual",
        observaciones: Optional[str] = None,
    ):
        if potencia_rx_dbm < Decimal("-50") or potencia_rx_dbm > Decimal("10"):
            raise ValueError("La potencia RX debe estar entre -50 y 10 dBm")

        lectura = LecturaOpticaModel(
            cliente_id=cliente.id,
            onu_id=cliente.onu_id,
            orden_id=orden_id,
            tecnico_id=tecnico_id,
            potencia_rx_dbm=potencia_rx_dbm,
            potencia_tx_dbm=potencia_tx_dbm,
            origen=origen,
            observaciones=observaciones,
        )
        self.db.add(lectura)
        await self.db.flush()
        return lectura

    async def _obtener_puerto_bloqueado(self, caja_nap_id: int, numero: int):
        caja = await self.db.get(CajaNapModel, caja_nap_id)
        if not caja:
            raise ValueError("La caja NAP no existe")
        if numero < 1 or numero > (caja.capacidad or 0):
            raise ValueError(
                f"El puerto debe estar entre 1 y {caja.capacidad}"
            )

        await self.sincronizar_puertos_nap(caja_nap_id)
        puerto = (
            await self.db.execute(
                select(PuertoNapModel)
                .where(
                    PuertoNapModel.caja_nap_id == caja_nap_id,
                    PuertoNapModel.numero == numero,
                )
                .with_for_update()
            )
        ).scalar_one()
        return puerto

    @staticmethod
    def _liberar_registro_puerto(puerto: PuertoNapModel, usuario_id: int):
        puerto.estado = "libre"
        puerto.cliente_id = None
        puerto.orden_id = None
        puerto.potencia_instalacion_dbm = None
        puerto.actualizado_por_id = usuario_id
