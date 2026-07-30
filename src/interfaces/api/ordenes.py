from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional
from uuid import uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.services.client_service import ClientService
from src.application.services.orden_service import OrdenService
from src.domain.schemas import InstalacionRequest
from src.infrastructure.auth import role_required
from src.infrastructure.database import get_db
from src.infrastructure.models import EvidenciaOrdenModel


router = APIRouter(prefix="/ordenes", tags=["Órdenes de servicio"])

UPLOAD_ROOT = (Path("uploads") / "ordenes").resolve()
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
}


def contenido_coincide_con_mime(contenido: bytes, mime_type: str) -> bool:
    if mime_type == "image/jpeg":
        return contenido.startswith(b"\xff\xd8\xff")
    if mime_type == "image/png":
        return contenido.startswith(b"\x89PNG\r\n\x1a\n")
    if mime_type == "image/webp":
        return (
            len(contenido) >= 12
            and contenido[:4] == b"RIFF"
            and contenido[8:12] == b"WEBP"
        )
    if mime_type == "application/pdf":
        return contenido.startswith(b"%PDF-")
    return False


class OrdenCrear(BaseModel):
    tipo: str = Field(
        pattern=r"^(instalacion|reparacion|cambio_domicilio|cambio_onu|retiro)$"
    )
    cliente_id: Optional[int] = None
    servicio_id: Optional[int] = Field(default=None, gt=0)
    caja_nap_sugerida_id: Optional[int] = None
    puerto_nap_sugerido: Optional[int] = Field(default=None, ge=1, le=128)
    prospecto_nombre: Optional[str] = Field(default=None, max_length=150)
    prospecto_telefono: Optional[str] = Field(default=None, max_length=20)
    prospecto_direccion: Optional[str] = Field(default=None, max_length=255)
    tecnico_id: Optional[int] = None
    prioridad: str = Field(
        default="normal",
        pattern=r"^(baja|normal|alta|urgente)$",
    )
    fecha_programada: Optional[datetime] = None
    motivo: Optional[str] = Field(default=None, max_length=100)
    descripcion: Optional[str] = None

    @model_validator(mode="after")
    def validar_destinatario(self):
        if not self.cliente_id and not self.prospecto_nombre:
            raise ValueError("Indica cliente_id o prospecto_nombre")
        return self


class OrdenActualizar(BaseModel):
    tecnico_id: Optional[int] = None
    prioridad: Optional[str] = Field(
        default=None,
        pattern=r"^(baja|normal|alta|urgente)$",
    )
    fecha_programada: Optional[datetime] = None
    motivo: Optional[str] = Field(default=None, max_length=100)
    descripcion: Optional[str] = None
    diagnostico: Optional[str] = None
    solucion: Optional[str] = None
    conformidad_cliente: Optional[bool] = None


class CambioEstadoOrden(BaseModel):
    estado: str = Field(
        pattern=r"^(pendiente|asignada|en_camino|trabajando|terminada|cancelada)$"
    )
    comentario: Optional[str] = Field(default=None, max_length=1000)
    version: int = Field(ge=1)


class MaterialCrear(BaseModel):
    descripcion: str = Field(min_length=2, max_length=150)
    cantidad: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    unidad: str = Field(default="pieza", min_length=1, max_length=30)
    observaciones: Optional[str] = Field(default=None, max_length=500)


class CierreInstalacionRequest(InstalacionRequest):
    solucion: str = Field(min_length=3)
    conformidad_cliente: bool
    version: int = Field(ge=1)


def _usuario_resumen(usuario):
    if not usuario:
        return None
    return {
        "id": usuario.id,
        "nombre": usuario.nombre_completo,
        "usuario": usuario.usuario,
    }


def serializar_orden(orden):
    return {
        "id": orden.id,
        "tipo": orden.tipo,
        "cliente_id": orden.cliente_id,
        "servicio_id": orden.servicio_id,
        "servicio": (
            {
                "id": orden.servicio.id,
                "alias": orden.servicio.alias,
                "direccion": orden.servicio.direccion,
                "estado": orden.servicio.estado,
            }
            if orden.servicio
            else None
        ),
        "caja_nap_sugerida_id": orden.caja_nap_sugerida_id,
        "puerto_nap_sugerido": orden.puerto_nap_sugerido,
        "cliente": (
            {
                "id": orden.cliente.id,
                "nombre": orden.cliente.nombre,
                "telefono": orden.cliente.telefono,
                "direccion": orden.cliente.direccion,
                "estado": orden.cliente.estado,
            }
            if orden.cliente
            else None
        ),
        "prospecto_nombre": orden.prospecto_nombre,
        "prospecto_telefono": orden.prospecto_telefono,
        "prospecto_direccion": orden.prospecto_direccion,
        "tecnico": _usuario_resumen(orden.tecnico),
        "creado_por": _usuario_resumen(orden.creado_por),
        "prioridad": orden.prioridad,
        "estado": orden.estado,
        "fecha_programada": orden.fecha_programada,
        "fecha_inicio": orden.fecha_inicio,
        "fecha_finalizacion": orden.fecha_finalizacion,
        "fecha_cancelacion": orden.fecha_cancelacion,
        "motivo": orden.motivo,
        "categoria_soporte": orden.categoria_soporte,
        "canal_reporte": orden.canal_reporte,
        "descripcion": orden.descripcion,
        "diagnostico": orden.diagnostico,
        "solucion": orden.solucion,
        "tiempo_primera_respuesta_minutos": (
            orden.tiempo_primera_respuesta_minutos
        ),
        "tiempo_resolucion_minutos": orden.tiempo_resolucion_minutos,
        "conformidad_cliente": orden.conformidad_cliente,
        "version": orden.version,
        "created_at": orden.created_at,
        "updated_at": orden.updated_at,
        "historial": [
            {
                "id": item.id,
                "estado_anterior": item.estado_anterior,
                "estado_nuevo": item.estado_nuevo,
                "comentario": item.comentario,
                "fecha": item.fecha,
                "usuario": _usuario_resumen(item.usuario),
            }
            for item in orden.historial
        ],
        "evidencias": [
            {
                "id": item.id,
                "tipo": item.tipo,
                "nombre_original": item.nombre_original,
                "mime_type": item.mime_type,
                "tamano_bytes": item.tamano_bytes,
                "comentario": item.comentario,
                "fecha": item.fecha,
                "url": f"/ordenes/{orden.id}/evidencias/{item.id}",
            }
            for item in orden.evidencias
        ],
        "materiales": [
            {
                "id": item.id,
                "descripcion": item.descripcion,
                "cantidad": item.cantidad,
                "unidad": item.unidad,
                "observaciones": item.observaciones,
            }
            for item in orden.materiales
        ],
        "diagnosticos_soporte": [
            {
                "id": item.id,
                "resultado": item.resultado,
                "codigo_sugerencia": item.codigo_sugerencia,
                "sugerencia": item.sugerencia,
                "pppoe_online": item.pppoe_online,
                "ping_estado": item.ping_estado,
                "perdida_paquetes_porcentaje": (
                    item.perdida_paquetes_porcentaje
                ),
                "trafico_subida_bps": item.trafico_subida_bps,
                "trafico_bajada_bps": item.trafico_bajada_bps,
                "onu_online": item.onu_online,
                "potencia_rx_dbm": item.potencia_rx_dbm,
                "potencia_tx_dbm": item.potencia_tx_dbm,
                "origen_olt": item.origen_olt,
                "errores": item.errores,
                "fecha": item.fecha,
                "ejecutado_por": _usuario_resumen(item.ejecutado_por),
            }
            for item in orden.diagnosticos_soporte
        ],
    }


def manejar_error(error):
    if isinstance(error, PermissionError):
        raise HTTPException(403, str(error))
    if isinstance(error, RuntimeError):
        raise HTTPException(409, str(error))
    raise HTTPException(400, str(error))


@router.get("/")
async def listar_ordenes(
    estado: Optional[str] = None,
    tipo: Optional[str] = None,
    tecnico_id: Optional[int] = None,
    cliente_id: Optional[int] = None,
    limite: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(
        role_required(["admin", "supervisor", "tecnico"])
    ),
):
    service = OrdenService(db)
    ordenes = await service.listar(
        current_user,
        estado,
        tipo,
        tecnico_id,
        cliente_id,
        limite,
    )
    return [serializar_orden(orden) for orden in ordenes]


@router.post("/", status_code=201)
async def crear_orden(
    datos: OrdenCrear,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    try:
        orden = await OrdenService(db).crear(datos, current_user)
        return serializar_orden(orden)
    except (ValueError, PermissionError, RuntimeError) as error:
        manejar_error(error)


@router.get("/{orden_id}")
async def obtener_orden(
    orden_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(
        role_required(["admin", "supervisor", "tecnico"])
    ),
):
    try:
        orden = await OrdenService(db).obtener(orden_id, current_user)
        return serializar_orden(orden)
    except (ValueError, PermissionError) as error:
        manejar_error(error)


@router.patch("/{orden_id}")
async def actualizar_orden(
    orden_id: int,
    datos: OrdenActualizar,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    try:
        orden = await OrdenService(db).actualizar(
            orden_id,
            datos,
            current_user,
        )
        return serializar_orden(orden)
    except (ValueError, PermissionError) as error:
        manejar_error(error)


@router.post("/{orden_id}/estado")
async def cambiar_estado(
    orden_id: int,
    datos: CambioEstadoOrden,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(
        role_required(["admin", "supervisor", "tecnico"])
    ),
):
    try:
        orden = await OrdenService(db).cambiar_estado(
            orden_id,
            datos.estado,
            datos.comentario,
            datos.version,
            current_user,
        )
        return serializar_orden(orden)
    except (ValueError, PermissionError, RuntimeError) as error:
        manejar_error(error)


@router.post("/{orden_id}/materiales", status_code=201)
async def agregar_material(
    orden_id: int,
    datos: MaterialCrear,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(
        role_required(["admin", "supervisor", "tecnico"])
    ),
):
    try:
        material = await OrdenService(db).agregar_material(
            orden_id,
            datos,
            current_user,
        )
        return {
            "id": material.id,
            "descripcion": material.descripcion,
            "cantidad": material.cantidad,
            "unidad": material.unidad,
            "observaciones": material.observaciones,
        }
    except (ValueError, PermissionError) as error:
        manejar_error(error)


@router.delete("/{orden_id}/materiales/{material_id}", status_code=204)
async def eliminar_material(
    orden_id: int,
    material_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(
        role_required(["admin", "supervisor", "tecnico"])
    ),
):
    try:
        await OrdenService(db).eliminar_material(
            orden_id,
            material_id,
            current_user,
        )
    except (ValueError, PermissionError) as error:
        manejar_error(error)


@router.post("/{orden_id}/evidencias", status_code=201)
async def subir_evidencia(
    orden_id: int,
    archivo: UploadFile = File(...),
    tipo: str = Form(default="foto"),
    comentario: Optional[str] = Form(default=None),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(
        role_required(["admin", "supervisor", "tecnico"])
    ),
):
    tipo = tipo.strip().lower()
    if tipo not in {"foto", "firma", "documento"}:
        raise HTTPException(400, "Tipo de evidencia inválido")
    if archivo.content_type not in MIME_EXTENSIONS:
        raise HTTPException(415, "Solo se permiten JPG, PNG, WEBP o PDF")

    contenido = await archivo.read(MAX_UPLOAD_BYTES + 1)
    if len(contenido) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "El archivo supera el límite de 10 MB")
    if not contenido:
        raise HTTPException(400, "El archivo está vacío")
    if not contenido_coincide_con_mime(contenido, archivo.content_type):
        raise HTTPException(415, "El contenido no coincide con el tipo de archivo")

    service = OrdenService(db)
    ruta = None
    try:
        orden = await service.obtener(orden_id, current_user)
        carpeta = UPLOAD_ROOT / str(orden.id)
        carpeta.mkdir(parents=True, exist_ok=True)
        ruta = carpeta / f"{uuid4().hex}{MIME_EXTENSIONS[archivo.content_type]}"
        ruta.write_bytes(contenido)
        evidencia = await service.agregar_evidencia(
            orden=orden,
            usuario=current_user,
            tipo=tipo,
            nombre_original=(archivo.filename or "evidencia")[:255],
            ruta_archivo=str(ruta),
            mime_type=archivo.content_type,
            tamano_bytes=len(contenido),
            comentario=comentario,
        )
        return {
            "id": evidencia.id,
            "tipo": evidencia.tipo,
            "nombre_original": evidencia.nombre_original,
            "tamano_bytes": evidencia.tamano_bytes,
            "url": f"/ordenes/{orden.id}/evidencias/{evidencia.id}",
        }
    except (ValueError, PermissionError) as error:
        if ruta and ruta.exists():
            ruta.unlink()
        manejar_error(error)
    except Exception:
        if ruta and ruta.exists():
            ruta.unlink()
        raise


@router.get("/{orden_id}/evidencias/{evidencia_id}")
async def descargar_evidencia(
    orden_id: int,
    evidencia_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(
        role_required(["admin", "supervisor", "tecnico"])
    ),
):
    try:
        await OrdenService(db).obtener(orden_id, current_user)
        evidencia = await db.get(EvidenciaOrdenModel, evidencia_id)
        if not evidencia or evidencia.orden_id != orden_id:
            raise ValueError("Evidencia no encontrada")
        ruta = Path(evidencia.ruta_archivo).resolve()
        if UPLOAD_ROOT not in ruta.parents or not ruta.is_file():
            raise ValueError("Archivo de evidencia no disponible")
        return FileResponse(
            ruta,
            media_type=evidencia.mime_type,
            filename=evidencia.nombre_original,
        )
    except (ValueError, PermissionError) as error:
        manejar_error(error)


@router.post("/{orden_id}/completar-instalacion")
async def completar_instalacion_guiada(
    orden_id: int,
    datos: CierreInstalacionRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(
        role_required(["admin", "supervisor", "tecnico"])
    ),
):
    orden_service = OrdenService(db)
    try:
        orden = await orden_service.obtener(orden_id, current_user)
        if orden.tipo != "instalacion" or not orden.cliente_id:
            raise ValueError("La orden no corresponde a una instalación")
        if orden.estado != "trabajando":
            raise ValueError("La orden debe estar en estado trabajando")
        if orden.version != datos.version:
            raise RuntimeError(
                "La orden cambió en otro dispositivo; actualiza antes de continuar"
            )

        client_service = ClientService(db)
        await client_service.activar_instalacion(
            orden.cliente_id,
            datos,
            usuario_operador=current_user,
            orden_id=orden.id,
        )
        orden.solucion = datos.solucion
        orden.conformidad_cliente = datos.conformidad_cliente
        await db.commit()
        orden = await orden_service.cambiar_estado(
            orden.id,
            "terminada",
            "Instalación y activación completadas",
            orden.version,
            current_user,
        )
        return serializar_orden(orden)
    except (ValueError, PermissionError, RuntimeError) as error:
        manejar_error(error)
