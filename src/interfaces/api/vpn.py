from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from src.application.services.vpn_service import VPNService
from src.domain.schemas import VpnTunnelCreate, VpnTunnelResponse
from src.infrastructure.database import get_db
from src.infrastructure.models import VpnTunnelModel

class VpnTecnicoCreate(BaseModel):
    nombre: str

router = APIRouter(prefix="/vpn", tags=["WireGuard VPN"])

@router.post("/tunnels/", response_model=VpnTunnelResponse)
async def crear_nuevo_tunel(datos: VpnTunnelCreate, db: AsyncSession = Depends(get_db)):
    """Crea un nuevo túnel VPN independiente"""
    service = VPNService(db)
    try:
        nuevo_tunel = await service.crear_tunel(nombre=datos.nombre)
        return nuevo_tunel
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/tunnels/", response_model=list[VpnTunnelResponse])
async def listar_tuneles(db: AsyncSession = Depends(get_db)):
    """Devuelve todos los túneles creados para mostrarlos en el Frontend"""
    result = await db.execute(select(VpnTunnelModel).order_by(VpnTunnelModel.id.desc()))
    return result.scalars().all()



@router.delete("/tunnels/{tunnel_id}")
async def eliminar_tunel(tunnel_id: int, db: AsyncSession = Depends(get_db)):
    """Elimina un túnel VPN de la base de datos para liberar la IP"""
    service = VPNService(db)
    
    # Buscamos el túnel
    result = await db.execute(select(VpnTunnelModel).where(VpnTunnelModel.id == tunnel_id))
    tunel = result.scalar_one_or_none()
    
    if not tunel:
        raise HTTPException(status_code=404, detail="Túnel no encontrado")
        
    try:
        # 1. Quitar el peer de Linux (Opcional, pero recomendado para mantener limpio wg0)
        service._ejecutar_comando([
            "sudo", "wg", "set", service.WG_INTERFACE, 
            "peer", tunel.public_key, 
            "remove"
        ])
        service._ejecutar_comando(["sudo", "wg-quick", "save", service.WG_INTERFACE])
    except Exception as e:
        print(f"No se pudo remover el peer de Linux: {e}")
        # Seguimos adelante para borrarlo de la BD de todos modos

    # 2. Borrar de la base de datos
    await db.delete(tunel)
    await db.commit()
    
    return {"message": "Túnel eliminado y la IP ha sido liberada"}



@router.post("/tecnicos/")
async def crear_vpn_para_tecnico(
    datos: VpnTecnicoCreate, 
    db: AsyncSession = Depends(get_db)
):
    """
    Crea un túnel VPN optimizado para Celulares (iOS/Android) o PCs (Windows/Mac).
    Devuelve la configuración en texto (.conf) y en imagen (Código QR en Base64).
    """
    service = VPNService(db)
    try:
        resultado = await service.crear_acceso_tecnico(datos.nombre)
        
        # Devolvemos un JSON con todo listo para que React lo dibuje
        return {
            "id": resultado["tunel"].id,
            "nombre": resultado["tunel"].nombre,
            "ip_asignada": resultado["tunel"].ip_asignada,
            "archivo_conf": resultado["archivo_conf"],
            "qr_imagen": resultado["qr_imagen"]  # <--- ¡La magia está aquí!
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))