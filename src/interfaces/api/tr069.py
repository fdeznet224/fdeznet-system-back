"""Infraestructura → TR-069: todos los equipos del ACS y la configuración de cada uno."""

import json
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.services import acs_nube_service
from src.application.services.config_onu_service import ConfigOnuService, ficha_equipo
from src.application.services.plantilla_tr069_service import (
    PlantillaTr069Service, cliente_de, clientes_por_equipo, desde_config, estado_dict, plantilla_dict, validar,
)
from src.application.services.red_cliente_service import url_nbi
from src.infrastructure.auth import role_required
from src.infrastructure.database import get_db
from src.infrastructure.models import EstadoEquipoTr069Model, PlantillaTr069Model

router = APIRouter(prefix="/tr069", tags=["TR-069"], dependencies=[Depends(role_required(["admin"]))])

PROYECCION = ",".join([
    "_id", "_lastInform", "_lastBoot", "_deviceId",
    "InternetGatewayDevice.ManagementServer.ConnectionRequestURL",
    "InternetGatewayDevice.DeviceInfo.SoftwareVersion",
])


def _error(error: Exception) -> HTTPException:
    if isinstance(error, LookupError):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, ValueError):
        return HTTPException(status_code=409, detail=str(error))
    return HTTPException(status_code=502, detail="No se pudo hablar con el ACS (GenieACS).")


@router.get("/acs")
async def acs_del_isp():
    """URL, usuario y clave del ACS para poner en los módems (en la nube, el del ISP)."""
    return acs_nube_service.datos()


@router.post("/acs/activar")
async def activar_acs():
    return await acs_nube_service.activar()


async def _fichas(db: AsyncSession, consulta: Optional[dict] = None) -> list:
    params = {"projection": PROYECCION}
    if consulta:
        params["query"] = json.dumps(consulta)
    try:
        async with httpx.AsyncClient(base_url=url_nbi(), timeout=30) as http:
            respuesta = await http.get("/devices", params=params)
            respuesta.raise_for_status()
            documentos = respuesta.json()
    except httpx.HTTPError as error:
        raise _error(error) from error
    mapas = await clientes_por_equipo(db)
    estados = {e.device_id: e for e in (await db.execute(select(EstadoEquipoTr069Model))).scalars().all()}
    plantillas = {p.id: p for p in (await db.execute(select(PlantillaTr069Model))).scalars().all()}
    salida = []
    for documento in documentos:
        ficha = ficha_equipo(documento)
        ficha["cliente"] = cliente_de(documento, mapas)
        estado = estados.get(documento.get("_id"))
        ficha["plantilla"] = estado_dict(estado, plantillas.get(estado.plantilla_id) if estado else None)
        salida.append(ficha)
    salida.sort(key=lambda e: e["ultimo_reporte"] or "", reverse=True)
    return salida


@router.get("/equipos")
async def equipos(db: AsyncSession = Depends(get_db)):
    """Todos los equipos que se reportan al ACS, con su cliente si se encuentra."""
    return await _fichas(db)


@router.get("/equipos/{device_id}")
async def equipo(device_id: str, db: AsyncSession = Depends(get_db)):
    encontrados = await _fichas(db, {"_id": device_id})
    if not encontrados:
        raise HTTPException(status_code=404, detail="Ese equipo no está en el ACS.")
    return encontrados[0]


class WanRequest(BaseModel):
    ruta: Optional[str] = None
    tipo: Optional[str] = None
    nombre: Optional[str] = None
    modo: Optional[str] = None
    vlan: Optional[int] = None
    servicio: Optional[str] = None
    habilitada: Optional[bool] = None
    usuario: Optional[str] = None
    clave: Optional[str] = None
    nat: Optional[bool] = None
    direccionamiento: Optional[str] = None
    ip: Optional[str] = None
    mascara: Optional[str] = None
    puerta_enlace: Optional[str] = None
    dns: Optional[str] = None
    puertos: Optional[list[str]] = None
    dmz: Optional[dict] = None


class EnlaceRequest(BaseModel):
    ruta: str
    puertos: list[str]


class WifiRequest(BaseModel):
    ssid: Optional[str] = None
    habilitada: Optional[bool] = None
    oculta: Optional[bool] = None
    canal: Optional[int] = None
    ancho: Optional[str] = None


class LanRequest(BaseModel):
    ip: str
    mascara: str = "255.255.255.0"
    dhcp: bool = True
    inicio: Optional[str] = None
    fin: Optional[str] = None
    tiempo: Optional[int] = None
    dns: Optional[str] = None


class PuertoRequest(BaseModel):
    wan: str
    externo: int
    interno: Optional[int] = None
    ip: str
    protocolo: str = "TCP"
    descripcion: Optional[str] = None


class RutaRequest(BaseModel):
    ruta: str


def _sin_nulos(modelo: BaseModel) -> dict:
    return {k: v for k, v in modelo.model_dump().items() if v is not None}


async def _hacer(trabajo):
    try:
        return await trabajo
    except (LookupError, ValueError, httpx.HTTPError) as error:
        raise _error(error) from error


@router.get("/equipos/{device_id}/config")
async def config(device_id: str, refrescar: bool = Query(True), db: AsyncSession = Depends(get_db)):
    """Lo que se ve al entrar a la web del módem: WAN, enlaces, WiFi y LAN, leído por TR-069."""
    return await _hacer(ConfigOnuService(db).leer(device_id, refrescar))


@router.post("/equipos/{device_id}/wan")
async def guardar_wan(device_id: str, datos: WanRequest, db: AsyncSession = Depends(get_db),
                      usuario=Depends(role_required(["admin"]))):
    """Con `ruta` edita esa WAN; sin ella crea una nueva (`tipo` pppoe o ip)."""
    campos = _sin_nulos(datos)
    ruta = campos.pop("ruta", None)
    servicio = ConfigOnuService(db)
    if ruta:
        campos.pop("tipo", None)
        return await _hacer(servicio.guardar_wan(device_id, ruta, campos, usuario))
    return await _hacer(servicio.agregar_wan(device_id, campos, usuario))


@router.post("/equipos/{device_id}/wan/borrar")
async def borrar_wan(device_id: str, datos: RutaRequest, db: AsyncSession = Depends(get_db),
                     usuario=Depends(role_required(["admin"]))):
    return await _hacer(ConfigOnuService(db).borrar_wan(device_id, datos.ruta, usuario))


@router.post("/equipos/{device_id}/enlaces")
async def enlaces(device_id: str, datos: EnlaceRequest, db: AsyncSession = Depends(get_db),
                  usuario=Depends(role_required(["admin"]))):
    return await _hacer(ConfigOnuService(db).guardar_enlaces(device_id, datos.ruta, datos.puertos, usuario))


@router.post("/equipos/{device_id}/wifi/{indice}")
async def wifi(device_id: str, indice: int, datos: WifiRequest, db: AsyncSession = Depends(get_db),
               usuario=Depends(role_required(["admin"]))):
    return await _hacer(ConfigOnuService(db).guardar_wifi(device_id, indice, _sin_nulos(datos), usuario))


@router.post("/equipos/{device_id}/lan")
async def lan(device_id: str, datos: LanRequest, db: AsyncSession = Depends(get_db),
              usuario=Depends(role_required(["admin"]))):
    return await _hacer(ConfigOnuService(db).guardar_lan(device_id, _sin_nulos(datos), usuario))


@router.post("/equipos/{device_id}/puertos")
async def abrir_puerto(device_id: str, datos: PuertoRequest, db: AsyncSession = Depends(get_db),
                       usuario=Depends(role_required(["admin"]))):
    return await _hacer(ConfigOnuService(db).abrir_puerto(device_id, _sin_nulos(datos), usuario))


@router.post("/equipos/{device_id}/puertos/cerrar")
async def cerrar_puerto(device_id: str, datos: RutaRequest, db: AsyncSession = Depends(get_db),
                        usuario=Depends(role_required(["admin"]))):
    return await _hacer(ConfigOnuService(db).cerrar_puerto(device_id, datos.ruta, usuario))


@router.post("/equipos/{device_id}/reiniciar")
async def reiniciar(device_id: str, db: AsyncSession = Depends(get_db)):
    servicio = ConfigOnuService(db)

    async def trabajo():
        async with servicio._http() as http:
            await servicio._existe(http, device_id)
            if not await servicio.red._tarea(http, device_id, {"name": "reboot"}):
                pendientes = (await http.get("/tasks", params={"query": json.dumps({"device": device_id, "name": "reboot"})})).json()
                for tarea in pendientes:
                    await http.delete(f"/tasks/{tarea['_id']}")
                raise ValueError("El equipo no contestó; intenta de nuevo en un momento.")
        return {"ok": True}

    return await _hacer(trabajo())


# ------------------------------------------------------------------ plantillas
class PlantillaRequest(BaseModel):
    nombre: str
    marca: str = "todas"
    modelos: str = ""
    automatica: bool = False
    definicion: dict


def _plantilla_valida(datos: PlantillaRequest) -> dict:
    if datos.marca not in ("todas", "huawei", "zte"):
        raise HTTPException(status_code=422, detail="La plantilla es para todas las marcas, Huawei o ZTE.")
    if not datos.nombre.strip():
        raise HTTPException(status_code=422, detail="Ponle nombre a la plantilla.")
    try:
        definicion = validar(datos.definicion)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    modelos = ",".join(m.strip().upper() for m in datos.modelos.split(",") if m.strip())
    return {"nombre": datos.nombre.strip()[:80], "marca": datos.marca, "modelos": modelos[:255],
            "automatica": datos.automatica, "definicion": json.dumps(definicion, ensure_ascii=False)}


@router.get("/plantillas")
async def plantillas(db: AsyncSession = Depends(get_db)):
    return [plantilla_dict(p) for p in await PlantillaTr069Service(db).plantillas()]


@router.post("/plantillas")
async def crear_plantilla(datos: PlantillaRequest, db: AsyncSession = Depends(get_db)):
    plantilla = PlantillaTr069Model(**_plantilla_valida(datos))
    db.add(plantilla)
    await db.commit()
    await db.refresh(plantilla)
    return plantilla_dict(plantilla)


@router.put("/plantillas/{plantilla_id}")
async def editar_plantilla(plantilla_id: int, datos: PlantillaRequest, db: AsyncSession = Depends(get_db)):
    plantilla = await db.get(PlantillaTr069Model, plantilla_id)
    if not plantilla:
        raise HTTPException(status_code=404, detail="Plantilla no encontrada.")
    for campo, valor in _plantilla_valida(datos).items():
        setattr(plantilla, campo, valor)
    await db.commit()
    await db.refresh(plantilla)
    return plantilla_dict(plantilla)


@router.delete("/plantillas/{plantilla_id}")
async def borrar_plantilla(plantilla_id: int, db: AsyncSession = Depends(get_db)):
    plantilla = await db.get(PlantillaTr069Model, plantilla_id)
    if not plantilla:
        raise HTTPException(status_code=404, detail="Plantilla no encontrada.")
    await db.delete(plantilla)
    await db.commit()
    return {"ok": True}


@router.get("/plantillas/desde-equipo/{device_id}")
async def plantilla_desde_equipo(device_id: str, db: AsyncSession = Depends(get_db)):
    """Lo que tiene ese equipo, como plantilla para copiarla a los de su modelo."""
    config_equipo = await _hacer(ConfigOnuService(db).leer(device_id))
    ficha = (await _fichas(db, {"_id": device_id}) or [{}])[0]
    return {"marca": "todas", "modelos": "", "modelo_origen": (ficha.get("modelo") or "").replace("%2D", "-"),
            "definicion": desde_config(config_equipo)}


@router.post("/equipos/{device_id}/plantilla/revisar")
async def revisar_plantilla(device_id: str, db: AsyncSession = Depends(get_db)):
    return await _hacer(PlantillaTr069Service(db).revisar_equipo(device_id))


@router.post("/equipos/{device_id}/plantilla/aplicar")
async def aplicar_plantilla(device_id: str, db: AsyncSession = Depends(get_db),
                            usuario=Depends(role_required(["admin"]))):
    return await _hacer(PlantillaTr069Service(db).aplicar_equipo(device_id, usuario))
