from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

# Infraestructura y Auth
from src.infrastructure.database import get_db
from src.infrastructure.auth import get_current_user # 🔒 Seguridad: Requiere Login

# Servicio
from src.application.services.dashboard_service import DashboardService

router = APIRouter(prefix="/dashboard", tags=["Dashboard & Métricas"])

@router.get("/home")
async def get_dashboard_home(
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Obtiene los KPIs principales para la pantalla de inicio:
    - Total de Clientes Activos
    - Finanzas del mes (Cobrado vs Pendiente)
    - Cortes programados para hoy
    - Tickets de soporte abiertos
    """
    service = DashboardService(db)
    try:
        return await service.obtener_home_data()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error cargando dashboard: {str(e)}")

@router.get("/clientes-online-detalle")
async def get_online_detalle(
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Métricas de estado de red en tiempo real:
    - Online (Navegando)
    - Offline (Posible caída de fibra/luz)
    - Morosos (Corte administrativo)
    """
    service = DashboardService(db)
    return await service.obtener_metricas_red()

@router.get("/status-tabla-clientes")
async def get_status_tabla_clientes(
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Devuelve un mapa de estados (ID Cliente -> Estado) para pintar
    la tabla de clientes con colores (Verde, Rojo, Naranja).
    """
    service = DashboardService(db)
    return await service.obtener_tabla_coloreada()