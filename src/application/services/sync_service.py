from datetime import datetime
from decimal import Decimal
import hashlib
import json
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.services.billing_service import BillingService
from src.application.services.orden_service import OrdenService
from src.application.services.support_service import SupportService
from src.infrastructure.models import (
    OperacionSincronizacionModel,
    UsuarioModel,
)


TIPOS_SINCRONIZABLES = {
    "orden_estado",
    "soporte_incidencia",
    "pago_factura",
}
ROLES_PAGO = {"admin", "supervisor", "cajero"}
ROLES_SOPORTE = {"admin", "supervisor", "cajero", "tecnico"}


class OrdenEstadoPayload(BaseModel):
    orden_id: int = Field(gt=0)
    estado: Literal["asignada", "en_camino", "trabajando"]
    version: int = Field(ge=1)
    comentario: Optional[str] = Field(default=None, max_length=1000)


class SoporteIncidenciaPayload(BaseModel):
    cliente_id: int = Field(gt=0)
    servicio_id: Optional[int] = Field(default=None, gt=0)
    categoria: Literal[
        "sin_internet",
        "lentitud",
        "potencia_baja",
        "router_wifi",
        "cable_roto",
        "cambio_domicilio",
        "otro",
    ]
    descripcion: str = Field(min_length=5, max_length=2000)
    tecnico_id: Optional[int] = Field(default=None, gt=0)
    prioridad: Optional[Literal["baja", "normal", "alta", "urgente"]] = None
    fecha_programada: Optional[datetime] = None
    canal_reporte: Literal[
        "panel",
        "telefono",
        "whatsapp",
        "presencial",
    ] = "panel"


class PagoFacturaPayload(BaseModel):
    factura_id: int = Field(gt=0)
    metodo_pago: Literal[
        "efectivo",
        "transferencia",
        "tarjeta",
        "deposito",
        "otro",
    ]
    monto_recibido: Decimal = Field(
        gt=0,
        max_digits=12,
        decimal_places=2,
    )
    referencia: Optional[str] = Field(default=None, max_length=100)


class SyncConflictError(RuntimeError):
    """La operación no puede repetirse con otro contenido."""


class SyncService:
    def __init__(self, db: AsyncSession):
        self.db = db

    @staticmethod
    def payload_hash(tipo: str, payload: dict[str, Any]) -> str:
        contenido = json.dumps(
            {"tipo": tipo, "payload": payload},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(contenido.encode("utf-8")).hexdigest()

    async def procesar(
        self,
        operacion_id: str,
        tipo: str,
        payload: dict[str, Any],
        creado_cliente: Optional[datetime],
        usuario: UsuarioModel,
    ) -> dict[str, Any]:
        if tipo not in TIPOS_SINCRONIZABLES:
            raise ValueError("Tipo de operación no sincronizable")

        payload_hash = self.payload_hash(tipo, payload)
        existente = await self.db.get(
            OperacionSincronizacionModel,
            operacion_id,
        )
        if existente:
            if (
                existente.usuario_id != usuario.id
                or existente.payload_hash != payload_hash
                or existente.tipo != tipo
            ):
                raise SyncConflictError(
                    "El identificador ya fue usado por otra operación"
                )
            return {
                "id": operacion_id,
                "estado": "repetida",
                "respuesta": json.loads(existente.respuesta),
            }

        if tipo == "orden_estado":
            respuesta = await self._cambiar_estado(payload, usuario)
        elif tipo == "soporte_incidencia":
            respuesta = await self._crear_incidencia(payload, usuario)
        else:
            respuesta = await self._registrar_pago(
                operacion_id,
                payload,
                usuario,
            )

        respuesta_json = json.dumps(
            respuesta,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        respuesta_normalizada = json.loads(respuesta_json)
        registro = OperacionSincronizacionModel(
            id=operacion_id,
            usuario_id=usuario.id,
            tipo=tipo,
            payload_hash=payload_hash,
            respuesta=respuesta_json,
            creado_cliente=creado_cliente,
        )
        self.db.add(registro)
        await self.db.commit()
        return {
            "id": operacion_id,
            "estado": "aplicada",
            "respuesta": respuesta_normalizada,
        }

    async def _cambiar_estado(
        self,
        payload: dict[str, Any],
        usuario: UsuarioModel,
    ) -> dict[str, Any]:
        datos = OrdenEstadoPayload.model_validate(payload)
        orden = await OrdenService(self.db).cambiar_estado(
            orden_id=datos.orden_id,
            nuevo_estado=datos.estado,
            comentario=datos.comentario,
            version=datos.version,
            usuario=usuario,
            commit=False,
        )
        return {
            "orden_id": orden.id,
            "estado": orden.estado,
            "version": orden.version,
        }

    async def _crear_incidencia(
        self,
        payload: dict[str, Any],
        usuario: UsuarioModel,
    ) -> dict[str, Any]:
        if usuario.rol not in ROLES_SOPORTE:
            raise PermissionError(
                "Tu rol no puede registrar incidencias de soporte"
            )
        datos = SoporteIncidenciaPayload.model_validate(payload)
        tecnico_id = datos.tecnico_id
        if usuario.rol == "tecnico":
            if tecnico_id and tecnico_id != usuario.id:
                raise PermissionError(
                    "Un técnico no puede asignar la incidencia a otra persona"
                )
            tecnico_id = usuario.id

        orden = await SupportService(self.db).crear_incidencia(
            cliente_id=datos.cliente_id,
            servicio_id=datos.servicio_id,
            categoria=datos.categoria,
            descripcion=datos.descripcion,
            usuario=usuario,
            tecnico_id=tecnico_id,
            prioridad=datos.prioridad,
            fecha_programada=datos.fecha_programada,
            canal_reporte=datos.canal_reporte,
            commit=False,
        )
        return {
            "orden_id": orden.id,
            "cliente_id": orden.cliente_id,
            "servicio_id": orden.servicio_id,
            "estado": orden.estado,
            "version": orden.version,
        }

    async def _registrar_pago(
        self,
        operacion_id: str,
        payload: dict[str, Any],
        usuario: UsuarioModel,
    ) -> dict[str, Any]:
        if usuario.rol not in ROLES_PAGO:
            raise PermissionError("Tu rol no puede registrar cobros")
        datos = PagoFacturaPayload.model_validate(payload)
        return await BillingService(self.db).registrar_pago_completo(
            factura_id=datos.factura_id,
            usuario_operador=usuario,
            metodo_pago=datos.metodo_pago,
            monto=datos.monto_recibido,
            referencia=datos.referencia,
            clave_idempotencia=operacion_id,
        )
