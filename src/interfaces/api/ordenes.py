from datetime import date, datetime
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

from src.application.services.activacion_service import ActivacionService, ActivacionTecnicoRequest
from src.application.services import chat_orden_service as chat_orden
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
    zona_id: Optional[int] = Field(default=None, gt=0)
    plan_id: Optional[int] = Field(default=None, gt=0)
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
    # Datos de la solicitud: lo que dijo el prospecto.
    prospecto_nombre: Optional[str] = Field(default=None, min_length=2, max_length=150)
    prospecto_telefono: Optional[str] = Field(default=None, max_length=20)
    prospecto_direccion: Optional[str] = Field(default=None, max_length=255)
    zona_id: Optional[int] = Field(default=None, gt=0)
    plan_id: Optional[int] = Field(default=None, gt=0)
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
                "latitud": orden.servicio.latitud,
                "longitud": orden.servicio.longitud,
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
                "latitud": orden.cliente.latitud,
                "longitud": orden.cliente.longitud,
            }
            if orden.cliente
            else None
        ),
        "prospecto_nombre": orden.prospecto_nombre,
        "prospecto_telefono": orden.prospecto_telefono,
        "prospecto_direccion": orden.prospecto_direccion,
        "zona_id": orden.zona_id,
        "plan_id": orden.plan_id,
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
    current_user=Depends(role_required(["admin", "supervisor", "tecnico"])),
):
    try:
        if current_user.rol == "tecnico":
            # El técnico solo levanta solicitudes de instalación y le quedan a él.
            if datos.tipo != "instalacion" or datos.cliente_id:
                raise PermissionError("El técnico solo puede registrar solicitudes de instalación")
            datos.tecnico_id = current_user.id
        orden = await OrdenService(db).crear(datos, current_user)
        return serializar_orden(orden)
    except (ValueError, PermissionError, RuntimeError) as error:
        manejar_error(error)


@router.get("/agenda")
async def agenda_tecnico(
    fecha: date,
    tecnico_id: Optional[int] = Query(default=None, gt=0),
    excluir_orden_id: Optional[int] = Query(default=None, gt=0),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    """Bloques de visita del día según el horario de atención y lo ya agendado al técnico."""
    from src.application.services.agenda_service import agenda_del_dia

    return await agenda_del_dia(db, tecnico_id, fecha, excluir_orden_id)


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


@router.get("/{orden_id}/activacion")
async def catalogo_activacion(
    orden_id: int,
    zona_id: Optional[int] = Query(default=None, gt=0),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor", "tecnico"])),
):
    """Todo lo que el técnico necesita para activar la solicitud, según la zona."""
    try:
        return await ActivacionService(db).catalogo(orden_id, current_user, zona_id)
    except (ValueError, PermissionError, RuntimeError) as error:
        manejar_error(error)


@router.post("/{orden_id}/activar")
async def activar_solicitud(
    orden_id: int,
    datos: ActivacionTecnicoRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["tecnico"])),
):
    """Solo el técnico da de alta: crea el cliente desde la solicitud, lo activa y cierra la orden."""
    try:
        return await ActivacionService(db).activar(orden_id, datos, current_user)
    except (ValueError, PermissionError, RuntimeError) as error:
        await db.rollback()
        manejar_error(error)


class MensajeOrden(BaseModel):
    mensaje: str = Field(min_length=1, max_length=4000)


@router.get("/chat/no-leidos")
async def no_leidos_de_ordenes(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    """Mensajes sin leer de cada orden de instalación abierta (clientes y prospectos)."""
    from sqlalchemy import select

    from src.infrastructure.models import OrdenServicioModel

    ordenes = (
        await db.execute(
            select(OrdenServicioModel).where(
                OrdenServicioModel.tipo == "instalacion",
                OrdenServicioModel.estado.notin_(["terminada", "cancelada"]),
            )
        )
    ).scalars().all()
    return await chat_orden.no_leidos_por_orden(db, ordenes)


@router.get("/{orden_id}/chat")
async def chat_de_orden(
    orden_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor", "tecnico"])),
):
    try:
        orden = await OrdenService(db).obtener(orden_id, current_user)
    except (ValueError, PermissionError) as error:
        manejar_error(error)
    return await chat_orden.mensajes_de_orden(db, orden)


@router.post("/{orden_id}/chat/enviar")
async def enviar_a_orden(
    orden_id: int,
    datos: MensajeOrden,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor", "tecnico"])),
):
    from src.infrastructure.models import ClienteModel
    from src.interfaces.api.whatsapp import encolar_mensaje_manual, telefono_whatsapp

    try:
        orden = await OrdenService(db).obtener(orden_id, current_user)
    except (ValueError, PermissionError) as error:
        manejar_error(error)
    cliente = await db.get(ClienteModel, orden.cliente_id) if orden.cliente_id else None
    telefono = telefono_whatsapp(cliente.telefono) if cliente and cliente.telefono else chat_orden.telefono_para_responder(orden)
    if not telefono:
        raise HTTPException(404, "La orden no tiene teléfono ni chat de WhatsApp")
    return await encolar_mensaje_manual(db, telefono, cliente.id if cliente else None, datos.mensaje, current_user)


@router.get("/{orden_id}/chat/archivo/{nombre}")
async def archivo_de_orden(
    orden_id: int,
    nombre: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor", "tecnico"])),
):
    """Adjuntos del chat de la orden, sin publicar el directorio de WhatsApp."""
    from sqlalchemy import select

    from src.infrastructure.models import MensajeChatModel

    try:
        orden = await OrdenService(db).obtener(orden_id, current_user)
    except (ValueError, PermissionError) as error:
        manejar_error(error)
    nombre_seguro = Path(nombre).name
    filtro = chat_orden.filtro_mensajes(orden)
    if nombre_seguro != nombre or not nombre_seguro or filtro is None:
        raise HTTPException(404, "Archivo no disponible")
    vinculado = await db.scalar(
        select(MensajeChatModel.id).where(filtro, MensajeChatModel.mensaje.contains(nombre_seguro)).limit(1)
    )
    uploads = (Path(__file__).resolve().parents[3] / "bot_whatsapp" / "uploads").resolve()
    archivo = (uploads / nombre_seguro).resolve()
    if vinculado is None or archivo.parent != uploads or not archivo.is_file() or archivo.is_symlink():
        raise HTTPException(404, "Archivo no disponible")
    return FileResponse(archivo)
