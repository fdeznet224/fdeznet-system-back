from typing import List, Optional
from datetime import date, datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, func, desc
from sqlalchemy.orm import joinedload
from pydantic import BaseModel

# Infraestructura
from src.infrastructure.database import get_db
from src.infrastructure.auth import get_current_user
from src.infrastructure.models import (
    FacturaModel, 
    ClienteModel, 
    PagoModel, 
    UsuarioModel
)

# Servicios
from src.application.services.billing_service import BillingService
from src.application.services.notification_service import NotificationService

router = APIRouter(prefix="/finanzas", tags=["Módulo Financiero"])

# ==========================================
# 0. SCHEMAS LOCALES (Input)
# ==========================================
class CobroFullRequest(BaseModel):
    factura_id: int
    metodo_pago: str  # efectivo, transferencia, etc.
    monto_recibido: float
    referencia: Optional[str] = None

class PromesaPagoRequest(BaseModel):
    factura_id: int
    nueva_fecha: date
    notas: Optional[str] = None


# ==========================================
# 1. LISTADO DE FACTURAS (Dashboard Financiero)
# ==========================================
@router.get("/listado-completo")
async def get_listado_completo(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    tipo_fecha: str = Query("emision"), 
    estado: str = Query("cualquiera"),   
    router_id: Optional[int] = None,
    cliente_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Lista facturas con filtros avanzados y cálculo de totales.
    """
    query = select(FacturaModel).options(
        joinedload(FacturaModel.cliente) 
    ).join(ClienteModel)

    # Seguridad
    if current_user.rol != 'admin':
        allowed_router_ids = [r.id for r in current_user.routers_asignados]
        if not allowed_router_ids:
            return {"items": [], "resumen": {"pagadas_cant": 0, "pagadas_total": 0, "pendientes_cant": 0, "pendientes_total": 0}}
        query = query.where(ClienteModel.router_id.in_(allowed_router_ids))

    # Filtros de Fecha
    if start_date and end_date:
        if tipo_fecha == "vencimiento":
            query = query.where(and_(FacturaModel.fecha_vencimiento >= start_date, FacturaModel.fecha_vencimiento <= end_date))
        else:
            query = query.where(and_(FacturaModel.fecha_emision >= start_date, FacturaModel.fecha_emision <= end_date))

    # Filtros Opcionales
    if router_id: query = query.where(ClienteModel.router_id == router_id)
    if cliente_id: query = query.where(FacturaModel.cliente_id == cliente_id)
    
    # Filtro de Estado
    if estado != "cualquiera":
        if estado == "pendiente":
            query = query.where(FacturaModel.estado == "pendiente")
        else:
            query = query.where(FacturaModel.estado == estado)

    # Ejecutar consulta
    result = await db.execute(query.order_by(desc(FacturaModel.id)))
    facturas = result.scalars().all()

    # Resumen
    resumen = {
        "pagadas_cant": 0, "pagadas_total": 0.0, 
        "pendientes_cant": 0, "pendientes_total": 0.0, 
        "vencidas_cant": 0, "vencidas_total": 0.0, 
        "anuladas_cant": 0, "anuladas_total": 0.0
    }
    today = date.today()
    items_response = []

    for f in facturas:
        valor = float(f.total) if f.total else 0.0
        
        if f.estado == "pagada":
            resumen["pagadas_cant"] += 1
            resumen["pagadas_total"] += valor
        elif f.estado == "anulada":
            resumen["anuladas_cant"] += 1
            resumen["anuladas_total"] += valor
        elif f.estado == "pendiente":
            fecha_limite = f.fecha_promesa_pago if (f.es_promesa_activa and f.fecha_promesa_pago) else f.fecha_vencimiento
            if fecha_limite and fecha_limite < today:
                resumen["vencidas_cant"] += 1
                resumen["vencidas_total"] += valor
            else:
                resumen["pendientes_cant"] += 1
                resumen["pendientes_total"] += valor
        
        factura_dict = {
            "id": f.id,
            "estado": f.estado,
            "saldo_pendiente": f.saldo_pendiente,
            "total": f.total,
            "fecha_emision": f.fecha_emision,
            "fecha_vencimiento": f.fecha_vencimiento,
            "plan_snapshot": f.plan_snapshot,
            "cliente": {
                "id": f.cliente.id,
                "nombre": f.cliente.nombre,
                "ip_asignada": f.cliente.ip_asignada,
                "sn": f.cliente.cedula  # Mapeado de Cédula a SN
            }
        }
        items_response.append(factura_dict)

    return {"items": items_response, "resumen": resumen}


# ==========================================
# 2. GENERACIÓN DE FACTURAS Y CORTES
# ==========================================

@router.post("/generar-masivo")
async def generar_masivo(db: AsyncSession = Depends(get_db)):
    """MODO AUTOMÁTICO (CRONJOB)"""
    service = BillingService(db)
    reporte = await service.generar_emision_masiva() 
    return {"mensaje": "Proceso automático finalizado", "detalles": reporte}

@router.post("/manual/generar-facturas/{dia_pago}")
async def generar_facturas_manual(
    dia_pago: int, 
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """MODO MANUAL (FORZADO)"""
    service = BillingService(db)
    try:
        resultado = await service.generar_emision_masiva(dia_objetivo=dia_pago)
        return {"status": "ok", "mensaje": f"Proceso manual finalizado para el Grupo de Pago día {dia_pago}", "detalles": resultado}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/ejecutar-cortes-emergencia")
async def forzar_cortes_ahora(db: AsyncSession = Depends(get_db)):
    """Botón de pánico para ejecutar cortes sin esperar al Cron"""
    service = BillingService(db)
    try:
        resultado = await service.procesar_cortes_automaticos()
        return {"status": "ok", "resultado": resultado}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==========================================
# 3. REGISTRAR COBRO (Caja)
# ==========================================
@router.post("/cobrar")
async def registrar_cobro(
    data: CobroFullRequest,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    service = BillingService(db)
    try:
        resultado = await service.registrar_pago_completo(
            factura_id=data.factura_id,
            usuario_operador=current_user,
            metodo_pago=data.metodo_pago,
            monto=data.monto_recibido,
            referencia=data.referencia
        )
        return resultado
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==========================================
# 4. PROMESA DE PAGO
# ==========================================
@router.post("/promesa-pago")
async def registrar_promesa(
    data: PromesaPagoRequest,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    factura = await db.get(FacturaModel, data.factura_id)
    if not factura: raise HTTPException(404, "Factura no encontrada")
    
    cliente = await db.get(ClienteModel, factura.cliente_id)

    factura.fecha_promesa_pago = data.nueva_fecha
    factura.es_promesa_activa = True
    
    reactivado = False
    if cliente.estado == 'suspendido':
        cliente.estado = 'activo'
        # Reactivamos en MikroTik instantáneamente
        service = BillingService(db)
        reactivado = await service._reactivar_en_mikrotik(cliente)

    await db.commit()

    return {
        "status": "ok",
        "mensaje": f"Promesa registrada hasta {data.nueva_fecha}",
        "reactivado": reactivado
    }


# ==========================================
# 5. REPORTE DE CAJA Y GRÁFICAS
# ==========================================
@router.get("/pagos-reporte")
async def obtener_reporte_caja(
    start_date: date,
    end_date: date,
    usuario_id: Optional[int] = Query(None),
    router_id: Optional[int] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    query = select(
        PagoModel.id,
        PagoModel.monto_total,
        PagoModel.metodo_pago,
        PagoModel.fecha_pago,
        PagoModel.factura_id,
        ClienteModel.nombre.label("cliente_nombre"),
        UsuarioModel.nombre_completo.label("usuario_nombre")
    ).join(ClienteModel, PagoModel.cliente_id == ClienteModel.id)\
     .outerjoin(UsuarioModel, PagoModel.usuario_id == UsuarioModel.id)

    query = query.where(func.date(PagoModel.fecha_pago) >= start_date)
    query = query.where(func.date(PagoModel.fecha_pago) <= end_date)

    if current_user.rol != 'admin':
        query = query.where(PagoModel.usuario_id == current_user.id)
    else:
        if usuario_id: query = query.where(PagoModel.usuario_id == usuario_id)

    if router_id:
        query = query.where(ClienteModel.router_id == router_id)

    result = await db.execute(query.order_by(desc(PagoModel.id)))
    pagos = result.all()

    total = sum([p.monto_total for p in pagos])

    return {
        "total_periodo": total,
        "detalles": [
            {
                "id": row.id,
                "monto": row.monto_total,
                "metodo": row.metodo_pago,
                "fecha": row.fecha_pago,
                "factura_id": row.factura_id,
                "cliente_nombre": row.cliente_nombre,
                "usuario_nombre": row.usuario_nombre or "Sistema"
            }
            for row in pagos
        ]
    }

@router.get("/estadisticas")
async def get_estadisticas(
    anio: int = Query(...),
    router_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    query = select(
        func.extract('month', FacturaModel.fecha_emision).label('mes'),
        func.sum(FacturaModel.total).label('total')
    ).where(func.extract('year', FacturaModel.fecha_emision) == anio)

    if current_user.rol != 'admin':
         allowed = [r.id for r in current_user.routers_asignados]
         if allowed:
             query = query.join(ClienteModel).where(ClienteModel.router_id.in_(allowed))
         else:
             return []
    elif router_id:
        query = query.join(ClienteModel).where(ClienteModel.router_id == router_id)
        
    query = query.group_by('mes').order_by('mes')
    result = await db.execute(query)
    data = result.all()
    
    return [{"mes": int(row.mes), "total": float(row.total)} for row in data]