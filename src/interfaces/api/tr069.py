"""Infraestructura → TR-069: todos los equipos del ACS y la configuración de cada uno."""

import json
import re
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.services.config_onu_service import ConfigOnuService, ficha_equipo, serial_reportado
from src.application.services.red_cliente_service import serial_tr069, url_nbi
from src.infrastructure.auth import role_required
from src.infrastructure.database import get_db
from src.infrastructure.models import ClienteModel, InventarioONUModel, ServicioModel

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


async def _clientes_por_equipo(db: AsyncSession) -> tuple:
    """({serie: cliente}, {ip: cliente}) para ligar cada equipo del ACS con su cliente."""
    por_serie: Dict[str, Dict[str, Any]] = {}
    por_ip: Dict[str, Dict[str, Any]] = {}
    filas = (await db.execute(
        select(ClienteModel.id, ClienteModel.nombre, ClienteModel.ip_asignada, InventarioONUModel.identificador)
        .outerjoin(InventarioONUModel, InventarioONUModel.id == ClienteModel.onu_id)
    )).all()
    filas += (await db.execute(
        select(ClienteModel.id, ClienteModel.nombre, ServicioModel.ip_asignada, InventarioONUModel.identificador)
        .join(ServicioModel, ServicioModel.cliente_id == ClienteModel.id)
        .outerjoin(InventarioONUModel, InventarioONUModel.id == ServicioModel.onu_id)
    )).all()
    for cliente_id, nombre, ip, identificador in filas:
        cliente = {"id": cliente_id, "nombre": nombre}
        if identificador:
            limpio = re.sub(r"[^0-9A-Za-z]", "", identificador).upper()
            for serie in filter(None, {limpio, serial_tr069(identificador)}):
                por_serie.setdefault(serie, cliente)
        if ip:
            por_ip.setdefault(ip.strip(), cliente)
    return por_serie, por_ip


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
    por_serie, por_ip = await _clientes_por_equipo(db)
    salida = []
    for documento in documentos:
        ficha = ficha_equipo(documento)
        ficha["cliente"] = por_serie.get(serial_reportado(documento)) or por_ip.get(ficha["ip"] or "")
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
