"""Panel del agente de IA: configuración y revisión de sugerencias."""

import json

import httpx
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.services.agente_ia_prompt import CONOCIMIENTO_INICIAL
from src.application.services.agente_ia_service import AgenteIAService
from src.application.services.bot_flow_service import get_or_create_bot_config
from src.infrastructure.auth import role_required
from src.infrastructure.database import get_db
from src.infrastructure.models import AgenteInteraccionModel
from src.infrastructure.whatsapp_client import WhatsAppService

router = APIRouter(prefix="/agente-ia", tags=["Agente de IA"])

ROLES_CHAT = ["admin", "supervisor", "tecnico"]


class ConfiguracionAgenteRequest(BaseModel):
    # Lo que no se envía se deja como está: el selector de quién contesta solo
    # cambia el modo y la pestaña del agente solo cambia el conocimiento.
    modo: Optional[Literal["apagado", "sugerencia", "automatico"]] = None
    conocimiento: Optional[str] = Field(default=None, max_length=20000)


class ConexionAgenteRequest(BaseModel):
    """Se captura en Configuración → Integraciones y claves."""

    url: str = Field(default="https://api.deepseek.com/v1", min_length=8, max_length=255)
    modelo: str = Field(default="deepseek-flash", min_length=2, max_length=80)
    # Solo escritura: vacío = conservar la clave guardada.
    api_key: Optional[str] = Field(default=None, max_length=500)


class AprobarRequest(BaseModel):
    texto: Optional[str] = Field(default=None, max_length=4000)


def _serializar(interaccion: AgenteInteraccionModel) -> dict:
    def cargar(valor):
        return json.loads(valor) if valor else []

    return {
        "id": interaccion.id,
        "telefono": interaccion.telefono,
        "cliente_id": interaccion.cliente_id,
        "modo": interaccion.modo,
        "estado": interaccion.estado,
        "mensaje_cliente": interaccion.mensaje_cliente,
        "respuesta_propuesta": interaccion.respuesta_propuesta,
        "respuesta_enviada": interaccion.respuesta_enviada,
        "consultas": [
            {"nombre": c["nombre"], "resultado": c.get("resultado")}
            for c in cargar(interaccion.herramientas_json)
        ],
        "acciones_pendientes": cargar(interaccion.acciones_pendientes_json),
        "acciones_resultado": cargar(interaccion.acciones_resultado_json),
        "error": interaccion.error,
        "costo_usd": float(interaccion.costo_usd or 0),
        "creado_en": interaccion.creado_en,
    }


@router.get("/configuracion")
async def obtener_configuracion(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    config = await get_or_create_bot_config(db)
    inicio_mes = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    por_estado = dict(
        (
            await db.execute(
                select(AgenteInteraccionModel.estado, func.count())
                .where(AgenteInteraccionModel.creado_en >= inicio_mes)
                .group_by(AgenteInteraccionModel.estado)
            )
        ).all()
    )
    costo_mes = (
        await db.execute(
            select(func.coalesce(func.sum(AgenteInteraccionModel.costo_usd), 0)).where(
                AgenteInteraccionModel.creado_en >= inicio_mes
            )
        )
    ).scalar_one()
    return {
        "modo": config.agente_modo,
        "url": config.agente_url,
        "modelo": config.agente_modelo,
        "tiene_clave": bool(config.agente_api_key),
        "conocimiento": config.agente_conocimiento or CONOCIMIENTO_INICIAL,
        "mes": {"por_estado": por_estado, "costo_usd": float(costo_mes or 0)},
    }


@router.put("/configuracion")
async def guardar_configuracion(
    datos: ConfiguracionAgenteRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin"])),
):
    config = await get_or_create_bot_config(db)
    if datos.modo is not None:
        if datos.modo != "apagado" and not config.agente_api_key:
            raise HTTPException(
                400,
                "Primero agrega la clave de IA en Configuración → Integraciones y claves",
            )
        config.agente_modo = datos.modo
    if datos.conocimiento is not None:
        config.agente_conocimiento = datos.conocimiento.strip() or None
    await db.commit()
    return {"status": "ok", "modo": config.agente_modo}


@router.get("/conexion")
async def obtener_conexion(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin"])),
):
    config = await get_or_create_bot_config(db)
    return {
        "url": config.agente_url,
        "modelo": config.agente_modelo,
        "tiene_clave": bool(config.agente_api_key),
    }


@router.put("/conexion")
async def guardar_conexion(
    datos: ConexionAgenteRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin"])),
):
    config = await get_or_create_bot_config(db)
    config.agente_url = datos.url.strip()
    config.agente_modelo = datos.modelo.strip()
    if datos.api_key and datos.api_key.strip():
        config.agente_api_key = datos.api_key.strip()
    await db.commit()
    return {"status": "ok", "tiene_clave": bool(config.agente_api_key)}


async def verificar_conexion_ia(url: str, modelo: str, clave: str, http: httpx.AsyncClient | None = None) -> dict:
    """Consulta la lista de modelos del proveedor (no cuesta tokens)."""
    cliente = http or httpx.AsyncClient(timeout=20)
    try:
        respuesta = await cliente.get(url.rstrip("/") + "/models", headers={"Authorization": "Bearer " + clave})
    except httpx.HTTPError as exc:
        return {"ok": False, "detalle": f"No se pudo conectar con el proveedor: {exc}"}
    finally:
        if http is None:
            await cliente.aclose()
    if respuesta.status_code in (401, 403):
        return {"ok": False, "detalle": "La clave no es válida o fue revocada."}
    if respuesta.status_code >= 400:
        return {"ok": False, "detalle": f"El proveedor respondió HTTP {respuesta.status_code}."}
    modelos = [m.get("id", "").replace("models/", "") for m in respuesta.json().get("data", [])]
    if modelo not in modelos:
        return {"ok": False, "detalle": f"La clave funciona, pero el modelo '{modelo}' no existe. Disponibles: {', '.join(modelos[:10])}"}
    return {"ok": True, "detalle": f"Conexión correcta con el modelo {modelo}."}


@router.post("/conexion/probar")
async def probar_conexion(
    datos: ConexionAgenteRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin"])),
):
    config = await get_or_create_bot_config(db)
    clave = (datos.api_key or "").strip() or (config.agente_api_key or "").strip()
    if not clave:
        raise HTTPException(400, "Escribe la clave de API para probarla")
    return await verificar_conexion_ia(datos.url.strip(), datos.modelo.strip(), clave)


@router.get("/sugerencias")
async def listar_sugerencias(
    cliente_id: Optional[int] = None,
    telefono: Optional[str] = None,
    estado: Optional[str] = "pendiente",
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(ROLES_CHAT)),
):
    consulta = select(AgenteInteraccionModel).order_by(AgenteInteraccionModel.id.desc()).limit(50)
    if estado:
        consulta = consulta.where(AgenteInteraccionModel.estado == estado)
    filtros = []
    if cliente_id:
        filtros.append(AgenteInteraccionModel.cliente_id == cliente_id)
    if telefono:
        filtros.append(AgenteInteraccionModel.telefono == telefono)
    if filtros:
        consulta = consulta.where(or_(*filtros))
    return [_serializar(i) for i in (await db.execute(consulta)).scalars().all()]


def _servicio(db: AsyncSession) -> AgenteIAService:
    async def enviar(telefono: str, mensaje: str) -> bool:
        return await WhatsAppService().enviar_mensaje(telefono=telefono, mensaje=mensaje, tipo_evento="agente_ia")

    return AgenteIAService(db, enviar=enviar)


@router.post("/sugerencias/{interaccion_id}/aprobar")
async def aprobar_sugerencia(
    interaccion_id: int,
    datos: AprobarRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(ROLES_CHAT)),
):
    try:
        interaccion = await _servicio(db).aprobar(interaccion_id, current_user, datos.texto)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _serializar(interaccion)


@router.post("/sugerencias/{interaccion_id}/descartar")
async def descartar_sugerencia(
    interaccion_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(ROLES_CHAT)),
):
    try:
        interaccion = await _servicio(db).descartar(interaccion_id, current_user)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _serializar(interaccion)
