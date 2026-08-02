from datetime import date, datetime, time, timedelta
from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from src.infrastructure.models import (
    ClienteModel,
    MensajeChatModel,
    UsuarioModel,
)
from src.infrastructure.whatsapp_client import (
    estado_por_ack,
    whatsapp_queue,
)


ESTADOS_SALIDA = {
    "pendiente",
    "procesando",
    "enviado",
    "entregado",
    "leido",
    "fallido",
    "incierto",
}
ESTADOS_REENVIABLES = {"fallido", "incierto"}


class WhatsAppOutboxService:
    def __init__(self, db: AsyncSession):
        self.db = db

    @staticmethod
    def _consulta_base():
        return select(MensajeChatModel).options(
            joinedload(MensajeChatModel.cliente),
            joinedload(MensajeChatModel.creado_por),
            joinedload(MensajeChatModel.ultimo_reintento_por),
        )

    @staticmethod
    def _filtros(
        *,
        estado: Optional[str] = None,
        tipo_evento: Optional[str] = None,
        cliente_id: Optional[int] = None,
        busqueda: Optional[str] = None,
        desde: Optional[date] = None,
        hasta: Optional[date] = None,
        lote_id: Optional[str] = None,
    ):
        filtros = [MensajeChatModel.direccion == "salida"]
        if estado:
            if estado not in ESTADOS_SALIDA:
                raise ValueError("Estado de envío inválido")
            filtros.append(MensajeChatModel.estado_envio == estado)
        if tipo_evento:
            filtros.append(MensajeChatModel.tipo_evento == tipo_evento)
        if cliente_id:
            filtros.append(MensajeChatModel.cliente_id == cliente_id)
        if lote_id:
            filtros.append(MensajeChatModel.lote_id == lote_id)
        if busqueda:
            termino = f"%{busqueda.strip()}%"
            filtros.append(
                or_(
                    MensajeChatModel.telefono.like(termino),
                    MensajeChatModel.mensaje.like(termino),
                    MensajeChatModel.cliente.has(
                        ClienteModel.nombre.like(termino)
                    ),
                )
            )
        if desde:
            filtros.append(
                MensajeChatModel.fecha
                >= datetime.combine(desde, time.min)
            )
        if hasta:
            filtros.append(
                MensajeChatModel.fecha
                < datetime.combine(hasta + timedelta(days=1), time.min)
            )
        return filtros

    async def listar(
        self,
        *,
        estado: Optional[str] = None,
        tipo_evento: Optional[str] = None,
        cliente_id: Optional[int] = None,
        busqueda: Optional[str] = None,
        desde: Optional[date] = None,
        hasta: Optional[date] = None,
        lote_id: Optional[str] = None,
        pagina: int = 1,
        limite: int = 50,
    ):
        filtros = self._filtros(
            estado=estado,
            tipo_evento=tipo_evento,
            cliente_id=cliente_id,
            busqueda=busqueda,
            desde=desde,
            hasta=hasta,
            lote_id=lote_id,
        )
        total = (
            await self.db.execute(
                select(func.count(MensajeChatModel.id)).where(*filtros)
            )
        ).scalar_one()
        registros = (
            await self.db.execute(
                self._consulta_base()
                .where(*filtros)
                .order_by(MensajeChatModel.id.desc())
                .offset((pagina - 1) * limite)
                .limit(limite)
            )
        ).scalars().unique().all()

        resumen_rows = (
            await self.db.execute(
                select(
                    MensajeChatModel.estado_envio,
                    func.count(MensajeChatModel.id),
                )
                .where(MensajeChatModel.direccion == "salida")
                .group_by(MensajeChatModel.estado_envio)
            )
        ).all()
        resumen = {estado: 0 for estado in sorted(ESTADOS_SALIDA)}
        for nombre, cantidad in resumen_rows:
            resumen[nombre or "pendiente"] = int(cantidad or 0)
        resumen["total"] = sum(
            cantidad
            for nombre, cantidad in resumen.items()
            if nombre != "total"
        )
        return registros, int(total), resumen

    async def obtener(self, mensaje_id: int):
        registro = (
            await self.db.execute(
                self._consulta_base().where(
                    MensajeChatModel.id == mensaje_id,
                    MensajeChatModel.direccion == "salida",
                )
            )
        ).scalar_one_or_none()
        if not registro:
            raise ValueError("Mensaje de salida no encontrado")
        return registro

    async def reintentar(
        self,
        mensaje_id: int,
        usuario: UsuarioModel,
    ):
        registro = (
            await self.db.execute(
                select(MensajeChatModel)
                .where(MensajeChatModel.id == mensaje_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if not registro or registro.direccion != "salida":
            raise ValueError("Mensaje de salida no encontrado")
        if registro.estado_envio not in ESTADOS_REENVIABLES:
            raise ValueError(
                "Solo se pueden reenviar mensajes fallidos o inciertos"
            )

        self._preparar_reintento(registro, usuario.id)
        await self.db.commit()
        await whatsapp_queue.agregar_tarea(
            {"mensaje_chat_id": registro.id, "intervalo": 0}
        )
        return await self.obtener(registro.id)

    async def reintentar_lote(
        self,
        usuario: UsuarioModel,
        ids: Optional[list[int]] = None,
        limite: int = 100,
    ):
        condiciones = [
            MensajeChatModel.direccion == "salida",
            MensajeChatModel.estado_envio.in_(ESTADOS_REENVIABLES),
        ]
        if ids is not None:
            if not ids:
                return []
            condiciones.append(MensajeChatModel.id.in_(set(ids)))
        registros = (
            await self.db.execute(
                select(MensajeChatModel)
                .where(*condiciones)
                .order_by(MensajeChatModel.id)
                .limit(limite)
                .with_for_update()
            )
        ).scalars().all()
        for registro in registros:
            self._preparar_reintento(registro, usuario.id)
        await self.db.commit()
        for registro in registros:
            await whatsapp_queue.agregar_tarea(
                {"mensaje_chat_id": registro.id, "intervalo": 0}
            )
        return [registro.id for registro in registros]

    @staticmethod
    def _preparar_reintento(
        registro: MensajeChatModel,
        usuario_id: int,
    ):
        registro.estado_envio = "pendiente"
        registro.ack = 0
        registro.wa_id = None
        registro.intentos = 0
        registro.ultimo_error = None
        registro.proximo_intento_en = datetime.now()
        registro.bloqueado_hasta = None
        registro.reintentos_manuales = (
            registro.reintentos_manuales or 0
        ) + 1
        registro.ultimo_reintento_por_id = usuario_id

    async def actualizar_ack(
        self,
        *,
        ack: int,
        wa_id: Optional[str] = None,
        mensaje_chat_id: Optional[int] = None,
    ):
        if mensaje_chat_id:
            registro = await self.db.get(
                MensajeChatModel,
                mensaje_chat_id,
            )
        elif wa_id:
            registro = (
                await self.db.execute(
                    select(MensajeChatModel).where(
                        MensajeChatModel.wa_id == wa_id
                    )
                )
            ).scalar_one_or_none()
        else:
            registro = None
        if not registro or registro.direccion != "salida":
            return None

        # Los eventos pueden llegar fuera de orden; nunca degradamos un ACK.
        ack_actual = registro.ack if registro.ack is not None else 0
        if ack_actual >= 0 and ack < ack_actual:
            return registro
        ahora = datetime.now()
        registro.ack = ack
        registro.estado_envio = estado_por_ack(ack)
        registro.wa_id = wa_id or registro.wa_id
        registro.bloqueado_hasta = None
        if ack >= 1:
            registro.enviado_en = registro.enviado_en or ahora
            registro.ultimo_error = None
            registro.proximo_intento_en = None
        if ack >= 2:
            registro.entregado_en = registro.entregado_en or ahora
        if ack >= 3:
            registro.leido_en = registro.leido_en or ahora
        if ack < 0:
            registro.ultimo_error = (
                registro.ultimo_error
                or "WhatsApp reportó un error de entrega"
            )
            if registro.intentos < registro.max_intentos:
                registro.proximo_intento_en = (
                    ahora + timedelta(minutes=1)
                )
        await self.db.commit()
        return registro
