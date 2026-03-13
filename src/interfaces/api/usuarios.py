from typing import List
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

# Infraestructura y Auth
from src.infrastructure.database import get_db
from src.infrastructure.auth import role_required # 🔒 Seguridad vital aquí

# Schemas (Unificados)
from src.domain.schemas import UsuarioCreate, UsuarioUpdate, UsuarioResponse

# Servicio
from src.application.services.user_service import UserService

router = APIRouter(prefix="/usuarios", tags=["Gestión de Staff (Usuarios)"])

# ==========================================
# GESTIÓN DE PERSONAL (SOLO ADMIN)
# ==========================================

@router.get("/", response_model=List[UsuarioResponse])
async def listar_usuarios(
    db: AsyncSession = Depends(get_db),
    current_user = Depends(role_required(["admin"])) # 🔒 Solo Admin ve la lista
):
    """
    Lista todos los empleados (Cajeros, Técnicos, Admins).
    """
    service = UserService(db)
    return await service.listar_usuarios()

@router.post("/", response_model=UsuarioResponse)
async def crear_usuario(
    datos: UsuarioCreate, 
    db: AsyncSession = Depends(get_db),
    current_user = Depends(role_required(["admin"])) # 🔒 Solo Admin crea
):
    """
    Registra un nuevo empleado.
    El servicio se encarga de hashear la contraseña.
    """
    service = UserService(db)
    try:
        return await service.crear_usuario(datos)
    except ValueError as e:
        # Ejemplo: "El usuario ya existe"
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error interno: {str(e)}")

@router.put("/{user_id}", response_model=UsuarioResponse)
async def editar_usuario(
    user_id: int, 
    datos: UsuarioUpdate, 
    db: AsyncSession = Depends(get_db),
    current_user = Depends(role_required(["admin"])) # 🔒 Solo Admin edita
):
    """
    Modifica datos de un empleado (Rol, Nombre, Contraseña, Routers asignados).
    """
    service = UserService(db)
    try:
        usuario = await service.editar_usuario(user_id, datos)
        if not usuario:
            raise HTTPException(status_code=404, detail="Usuario no encontrado")
        return usuario
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/{user_id}")
async def eliminar_usuario(
    user_id: int, 
    db: AsyncSession = Depends(get_db),
    current_user = Depends(role_required(["admin"])) # 🔒 Solo Admin elimina
):
    """
    Elimina (o desactiva) un empleado.
    """
    service = UserService(db)
    try:
        exito = await service.eliminar_usuario(user_id)
        if not exito:
            raise HTTPException(status_code=404, detail="Usuario no encontrado")
        return {"status": "success", "message": "Usuario eliminado correctamente"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))