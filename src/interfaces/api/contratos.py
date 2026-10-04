from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.services.contrato_service import VIGENCIA_APARTADO, ContratoService
from src.infrastructure.auth import role_required
from src.infrastructure.database import get_db

router = APIRouter(prefix="/contratos", tags=["Contratos"])
ROLES_INSTALACION = ["admin", "supervisor", "tecnico"]


def _serializar(apartado):
    return {
        "codigo": apartado.codigo,
        "reservado_en": apartado.reservado_en,
        "vence_en": apartado.reservado_en + VIGENCIA_APARTADO,
    }


@router.get("/apartados")
async def mis_contratos_apartados(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(ROLES_INSTALACION)),
):
    """Contratos apartados a tu nombre: el primero es el siguiente a usar."""
    apartados = await ContratoService(db).apartados(current_user.id)
    return {"apartados": [_serializar(a) for a in apartados]}


@router.post("/apartados/{codigo}/descartar")
async def descartar_contrato_apartado(
    codigo: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(ROLES_INSTALACION)),
):
    """Libera un apartado que no se usará (por ejemplo, la instalación no se hizo)."""
    servicio = ContratoService(db)
    try:
        await servicio.descartar(current_user.id, codigo)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"apartados": [_serializar(a) for a in await servicio.apartados(current_user.id)]}
