from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.infrastructure.models import (
    ClienteModel,
    DiagnosticoSoporteModel,
    EvidenciaOrdenModel,
    HistorialEstadoOrdenModel,
    MaterialOrdenModel,
    OrdenServicioModel,
    UsuarioModel,
)


TIPOS_ORDEN = {
    "instalacion",
    "reparacion",
    "cambio_domicilio",
    "cambio_onu",
    "retiro",
}
PRIORIDADES = {"baja", "normal", "alta", "urgente"}
ESTADOS_TERMINALES = {"terminada", "cancelada"}
TRANSICIONES = {
    "pendiente": {"asignada", "cancelada"},
    "asignada": {"pendiente", "en_camino", "trabajando", "cancelada"},
    "en_camino": {"asignada", "trabajando", "cancelada"},
    "trabajando": {"asignada", "terminada", "cancelada"},
    "terminada": set(),
    "cancelada": set(),
}


class OrdenService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def crear(
        self,
        datos,
        usuario: UsuarioModel,
        commit: bool = True,
    ):
        tipo = datos.tipo.strip().lower()
        prioridad = datos.prioridad.strip().lower()
        if tipo not in TIPOS_ORDEN:
            raise ValueError("Tipo de orden inválido")
        if prioridad not in PRIORIDADES:
            raise ValueError("Prioridad inválida")
        if not datos.cliente_id and not datos.prospecto_nombre:
            raise ValueError("Indica un cliente o el nombre del prospecto")
        if datos.cliente_id and not await self.db.get(ClienteModel, datos.cliente_id):
            raise ValueError("El cliente no existe")

        tecnico = await self._validar_tecnico(datos.tecnico_id)
        estado = "asignada" if tecnico else "pendiente"
        orden = OrdenServicioModel(
            tipo=tipo,
            cliente_id=datos.cliente_id,
            caja_nap_sugerida_id=datos.caja_nap_sugerida_id,
            puerto_nap_sugerido=datos.puerto_nap_sugerido,
            prospecto_nombre=datos.prospecto_nombre,
            prospecto_telefono=datos.prospecto_telefono,
            prospecto_direccion=datos.prospecto_direccion,
            tecnico_id=tecnico.id if tecnico else None,
            creado_por_id=usuario.id,
            prioridad=prioridad,
            estado=estado,
            fecha_programada=datos.fecha_programada,
            motivo=datos.motivo,
            descripcion=datos.descripcion,
        )
        self.db.add(orden)
        await self.db.flush()
        self._registrar_estado(
            orden,
            usuario.id,
            None,
            estado,
            "Orden creada",
        )
        if commit:
            await self.db.commit()
        return await self.obtener(orden.id)

    async def listar(
        self,
        usuario: UsuarioModel,
        estado: Optional[str] = None,
        tipo: Optional[str] = None,
        tecnico_id: Optional[int] = None,
        cliente_id: Optional[int] = None,
        limite: int = 100,
    ):
        stmt = self._consulta_base()
        if usuario.rol == "tecnico":
            stmt = stmt.where(OrdenServicioModel.tecnico_id == usuario.id)
        elif tecnico_id:
            stmt = stmt.where(OrdenServicioModel.tecnico_id == tecnico_id)
        if estado:
            stmt = stmt.where(OrdenServicioModel.estado == estado)
        if tipo:
            stmt = stmt.where(OrdenServicioModel.tipo == tipo)
        if cliente_id:
            stmt = stmt.where(OrdenServicioModel.cliente_id == cliente_id)

        resultado = await self.db.execute(
            stmt.order_by(
                OrdenServicioModel.fecha_programada.is_(None),
                OrdenServicioModel.fecha_programada,
                OrdenServicioModel.id.desc(),
            ).limit(limite)
        )
        return resultado.scalars().unique().all()

    async def obtener(
        self,
        orden_id: int,
        usuario: Optional[UsuarioModel] = None,
    ):
        orden = (
            await self.db.execute(
                self._consulta_base().where(OrdenServicioModel.id == orden_id)
            )
        ).scalar_one_or_none()
        if not orden:
            raise ValueError("Orden no encontrada")
        if usuario:
            self.validar_acceso(orden, usuario)
        return orden

    async def actualizar(
        self,
        orden_id: int,
        datos,
        usuario: UsuarioModel,
    ):
        orden = await self.obtener(orden_id, usuario)
        if orden.estado in ESTADOS_TERMINALES:
            raise ValueError("No se puede modificar una orden cerrada")
        if usuario.rol == "tecnico":
            raise PermissionError("El técnico solo puede actualizar el avance")

        cambios = datos.model_dump(exclude_unset=True)
        if "prioridad" in cambios:
            prioridad = cambios["prioridad"].strip().lower()
            if prioridad not in PRIORIDADES:
                raise ValueError("Prioridad inválida")
            orden.prioridad = prioridad
        if "tecnico_id" in cambios:
            tecnico = await self._validar_tecnico(cambios["tecnico_id"])
            orden.tecnico_id = tecnico.id if tecnico else None
            destino = "asignada" if tecnico else "pendiente"
            if destino != orden.estado:
                anterior = orden.estado
                orden.estado = destino
                self._registrar_estado(
                    orden,
                    usuario.id,
                    anterior,
                    destino,
                    "Asignación actualizada",
                )
        for campo in [
            "fecha_programada",
            "motivo",
            "descripcion",
            "diagnostico",
            "solucion",
            "conformidad_cliente",
        ]:
            if campo in cambios:
                setattr(orden, campo, cambios[campo])
        orden.version += 1
        await self.db.commit()
        return await self.obtener(orden.id)

    async def cambiar_estado(
        self,
        orden_id: int,
        nuevo_estado: str,
        comentario: Optional[str],
        version: int,
        usuario: UsuarioModel,
        commit: bool = True,
    ):
        orden = (
            await self.db.execute(
                select(OrdenServicioModel)
                .where(OrdenServicioModel.id == orden_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if not orden:
            raise ValueError("Orden no encontrada")
        self.validar_acceso(orden, usuario)
        if orden.version != version:
            raise RuntimeError(
                "La orden cambió en otro dispositivo; actualiza antes de continuar"
            )

        nuevo_estado = nuevo_estado.strip().lower()
        self.validar_transicion(orden.estado, nuevo_estado)
        if nuevo_estado == "terminada":
            await self._validar_cierre(orden)

        anterior = orden.estado
        orden.estado = nuevo_estado
        orden.version += 1
        ahora = datetime.now()
        if nuevo_estado == "trabajando" and not orden.fecha_inicio:
            orden.fecha_inicio = ahora
            orden.tiempo_primera_respuesta_minutos = self._minutos_desde(
                orden.created_at,
                ahora,
            )
        elif nuevo_estado == "terminada":
            orden.fecha_finalizacion = ahora
            orden.tiempo_resolucion_minutos = self._minutos_desde(
                orden.created_at,
                ahora,
            )
        elif nuevo_estado == "cancelada":
            orden.fecha_cancelacion = ahora

        self._registrar_estado(
            orden,
            usuario.id,
            anterior,
            nuevo_estado,
            comentario,
        )
        if commit:
            await self.db.commit()
        else:
            await self.db.flush()
        return await self.obtener(orden.id)

    async def agregar_material(
        self,
        orden_id: int,
        datos,
        usuario: UsuarioModel,
    ):
        orden = await self.obtener(orden_id, usuario)
        if orden.estado in ESTADOS_TERMINALES:
            raise ValueError("No se puede modificar una orden cerrada")
        material = MaterialOrdenModel(
            orden_id=orden.id,
            descripcion=datos.descripcion.strip(),
            cantidad=datos.cantidad,
            unidad=datos.unidad.strip(),
            observaciones=datos.observaciones,
        )
        self.db.add(material)
        orden.version += 1
        await self.db.commit()
        return material

    async def eliminar_material(
        self,
        orden_id: int,
        material_id: int,
        usuario: UsuarioModel,
    ):
        orden = await self.obtener(orden_id, usuario)
        if orden.estado in ESTADOS_TERMINALES:
            raise ValueError("No se puede modificar una orden cerrada")
        material = await self.db.get(MaterialOrdenModel, material_id)
        if not material or material.orden_id != orden.id:
            raise ValueError("Material no encontrado")
        await self.db.delete(material)
        orden.version += 1
        await self.db.commit()

    async def agregar_evidencia(
        self,
        orden: OrdenServicioModel,
        usuario: UsuarioModel,
        tipo: str,
        nombre_original: str,
        ruta_archivo: str,
        mime_type: str,
        tamano_bytes: int,
        comentario: Optional[str],
    ):
        self.validar_acceso(orden, usuario)
        if orden.estado in ESTADOS_TERMINALES:
            raise ValueError("No se puede agregar evidencia a una orden cerrada")
        evidencia = EvidenciaOrdenModel(
            orden_id=orden.id,
            usuario_id=usuario.id,
            tipo=tipo,
            nombre_original=nombre_original,
            ruta_archivo=ruta_archivo,
            mime_type=mime_type,
            tamano_bytes=tamano_bytes,
            comentario=comentario,
        )
        self.db.add(evidencia)
        orden.version += 1
        await self.db.commit()
        await self.db.refresh(evidencia)
        return evidencia

    @staticmethod
    def validar_acceso(orden: OrdenServicioModel, usuario: UsuarioModel):
        if usuario.rol == "tecnico" and orden.tecnico_id != usuario.id:
            raise PermissionError("La orden está asignada a otro técnico")

    async def _validar_tecnico(self, tecnico_id: Optional[int]):
        if not tecnico_id:
            return None
        tecnico = await self.db.get(UsuarioModel, tecnico_id)
        if not tecnico or not tecnico.activo or tecnico.rol != "tecnico":
            raise ValueError("El técnico indicado no existe o no está activo")
        return tecnico

    async def _validar_cierre(self, orden: OrdenServicioModel):
        if not orden.solucion:
            raise ValueError("Registra la solución antes de terminar la orden")
        evidencia = (
            await self.db.execute(
                select(EvidenciaOrdenModel.id)
                .where(EvidenciaOrdenModel.orden_id == orden.id)
                .limit(1)
            )
        ).first()
        if not evidencia:
            tiene_diagnostico = (
                await self.db.execute(
                    select(DiagnosticoSoporteModel.id)
                    .where(DiagnosticoSoporteModel.orden_id == orden.id)
                    .limit(1)
                )
            ).first()
            if orden.tipo != "reparacion" or not tiene_diagnostico:
                raise ValueError(
                    "Agrega una evidencia o ejecuta un diagnóstico antes de cerrar"
                )
        if orden.tipo == "instalacion" and not orden.conformidad_cliente:
            raise ValueError("Confirma la conformidad del cliente")

    @staticmethod
    def validar_transicion(estado_actual: str, estado_nuevo: str):
        if estado_nuevo not in TRANSICIONES.get(estado_actual, set()):
            raise ValueError(
                f"No se permite pasar de {estado_actual} a {estado_nuevo}"
            )

    def _registrar_estado(
        self,
        orden,
        usuario_id,
        estado_anterior,
        estado_nuevo,
        comentario,
    ):
        self.db.add(
            HistorialEstadoOrdenModel(
                orden_id=orden.id,
                usuario_id=usuario_id,
                estado_anterior=estado_anterior,
                estado_nuevo=estado_nuevo,
                comentario=comentario,
            )
        )

    @staticmethod
    def _minutos_desde(inicio, fin) -> int:
        if not inicio:
            return 0
        if getattr(inicio, "tzinfo", None) and not getattr(fin, "tzinfo", None):
            fin = fin.replace(tzinfo=inicio.tzinfo)
        elif getattr(fin, "tzinfo", None) and not getattr(inicio, "tzinfo", None):
            inicio = inicio.replace(tzinfo=fin.tzinfo)
        return max(0, int((fin - inicio).total_seconds() // 60))

    @staticmethod
    def _consulta_base():
        return select(OrdenServicioModel).options(
            selectinload(OrdenServicioModel.cliente),
            selectinload(OrdenServicioModel.tecnico),
            selectinload(OrdenServicioModel.creado_por),
            selectinload(OrdenServicioModel.historial).selectinload(
                HistorialEstadoOrdenModel.usuario
            ),
            selectinload(OrdenServicioModel.evidencias),
            selectinload(OrdenServicioModel.materiales),
            selectinload(OrdenServicioModel.diagnosticos_soporte).selectinload(
                DiagnosticoSoporteModel.ejecutado_por
            ),
        )
