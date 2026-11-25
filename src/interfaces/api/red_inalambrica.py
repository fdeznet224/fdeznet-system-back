"""Red inalámbrica: AP, PtP y demás equipos, su árbol y su monitoreo."""

import ipaddress
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.services.red_inalambrica_service import RedInalambricaService
from src.infrastructure.auth import role_required
from src.infrastructure.database import get_db

router = APIRouter(prefix="/red-inalambrica", tags=["Red inalámbrica"])
ven = role_required(["admin", "supervisor", "tecnico"])
editan = role_required(["admin"])


class Equipo(BaseModel):
    nombre: str = Field(min_length=1, max_length=100)
    tipo: str = "ap"
    marca: str = "ubiquiti"
    modelo: Optional[str] = Field(default=None, max_length=100)
    ip: str
    router_id: Optional[int] = None
    padre_id: Optional[int] = None
    troncal: bool = False
    router_hijo_id: Optional[int] = None
    zona_id: Optional[int] = None
    ubicacion: Optional[str] = Field(default=None, max_length=200)
    latitud: Optional[float] = None
    longitud: Optional[float] = None
    usuario: Optional[str] = Field(default=None, max_length=60)
    # Vacía conserva la que ya estaba guardada.
    clave: Optional[str] = Field(default=None, max_length=120)
    puerto_web: str = Field(default="443", pattern=r"^\d{1,5}$")
    activo: bool = True

    @field_validator("ip")
    @classmethod
    def validar_ip(cls, valor):
        try:
            return str(ipaddress.IPv4Address(valor.strip()))
        except ValueError as exc:
            raise ValueError("La IP no es válida (ej. 10.20.30.2)") from exc


def _error(error: Exception) -> HTTPException:
    if isinstance(error, ValueError):
        return HTTPException(status_code=409, detail=str(error))
    return HTTPException(status_code=502, detail="El MikroTik no respondió; intenta en un momento.")


class Padre(BaseModel):
    equipo_id: Optional[int] = None


@router.put("/olts/{olt_id}/padre")
async def colgar_olt(olt_id: int, datos: Padre, db: AsyncSession = Depends(get_db), _u=Depends(editan)):
    """De qué equipo cuelga la OLT en la topología (vacío: de su MikroTik)."""
    try:
        await RedInalambricaService(db).colgar_olt(olt_id, datos.equipo_id)
    except ValueError as error:
        raise _error(error) from error
    return {"ok": True}


@router.put("/routers/{router_id}/padre")
async def colgar_router(router_id: int, datos: Padre, db: AsyncSession = Depends(get_db), _u=Depends(editan)):
    """De qué equipo cuelga el MikroTik en la topología (vacío: suelto arriba)."""
    try:
        await RedInalambricaService(db).colgar_router(router_id, datos.equipo_id)
    except ValueError as error:
        raise _error(error) from error
    return {"ok": True}


@router.get("/arbol")
async def arbol(db: AsyncSession = Depends(get_db), _u=Depends(ven)):
    return await RedInalambricaService(db).arbol()


@router.post("/equipos", status_code=201)
async def crear(datos: Equipo, db: AsyncSession = Depends(get_db), _u=Depends(editan)):
    try:
        return await RedInalambricaService(db).guardar(datos.model_dump())
    except ValueError as error:
        raise _error(error) from error


@router.put("/equipos/{equipo_id}")
async def editar(equipo_id: int, datos: Equipo, db: AsyncSession = Depends(get_db), _u=Depends(editan)):
    try:
        return await RedInalambricaService(db).guardar(datos.model_dump(), equipo_id)
    except ValueError as error:
        raise _error(error) from error


@router.delete("/equipos/{equipo_id}", status_code=204)
async def borrar(equipo_id: int, db: AsyncSession = Depends(get_db), _u=Depends(editan)):
    try:
        await RedInalambricaService(db).borrar(equipo_id)
    except ValueError as error:
        raise _error(error) from error


@router.post("/equipos/{equipo_id}/revisar")
async def revisar(equipo_id: int, db: AsyncSession = Depends(get_db), _u=Depends(ven)):
    """Ping y lectura de airOS al momento."""
    try:
        return await RedInalambricaService(db).revisar(equipo_id)
    except Exception as error:
        raise _error(error) from error


@router.post("/equipos/{equipo_id}/abrir")
async def abrir(equipo_id: int, db: AsyncSession = Depends(get_db), _u=Depends(ven)):
    """Su página web: directo por la VPN o por un puente de 30 minutos en su MikroTik."""
    try:
        return await RedInalambricaService(db).abrir(equipo_id)
    except Exception as error:
        raise _error(error) from error
