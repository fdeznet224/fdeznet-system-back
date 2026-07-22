from typing import List, Optional
from datetime import date, datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, func, desc
from sqlalchemy.orm import joinedload
from pydantic import BaseModel
from sqlalchemy import select, and_, or_, func, desc

# Infraestructura
from src.infrastructure.database import get_db
from src.infrastructure.auth import get_current_user
from src.infrastructure.models import (
    FacturaModel, 
    ClienteModel, 
    PagoModel, 
    UsuarioModel,
    ServicioModel,
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


class FacturaManualRequest(BaseModel):
    cliente_id: int
    concepto: str
    monto: float
    descripcion: Optional[str] = None
    fecha_vencimiento: Optional[date] = None
    afecta_corte: bool = False


# ==========================================
# 1. LISTADO DE FACTURAS (Dashboard Financiero)
# ==========================================
@router.get("/listado-completo")
async def get_listado_completo(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    tipo_fecha: str = Query("vencimiento"), # 🔥 Por defecto ya pide vencimiento en el back
    estado: str = Query("cualquiera"),   
    router_id: Optional[int] = None,
    cliente_id: Optional[int] = None,
    busqueda: Optional[str] = None, 
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Lista facturas con filtros avanzados, cálculo de totales y protección anti-trampa de fechas.
    """
    query = select(FacturaModel).options(
        joinedload(FacturaModel.cliente) 
    ).join(ClienteModel)

    # Seguridad: Cajeros solo ven sus routers
    if current_user.rol != 'admin':
        allowed_router_ids = [r.id for r in current_user.routers_asignados]
        if not allowed_router_ids:
            return {"items": [], "resumen": {"pagadas_cant": 0, "pagadas_total": 0, "pendientes_cant": 0, "pendientes_total": 0}}
        query = query.where(ClienteModel.router_id.in_(allowed_router_ids))

    # 🔥 LA MAGIA: Solución al "Mes Fantasma" y filtro de Caja
    if start_date and end_date and not cliente_id and not busqueda:
        if tipo_fecha == "vencimiento":
            query = query.where(and_(FacturaModel.fecha_vencimiento >= start_date, FacturaModel.fecha_vencimiento <= end_date))
        elif tipo_fecha == "pago":
            # 🚀 NUEVO: Comparamos extrayendo solo el día (func.date) para ignorar la hora de la transacción
            query = query.where(and_(
                func.date(FacturaModel.fecha_pago_real) >= start_date, 
                func.date(FacturaModel.fecha_pago_real) <= end_date
            ))
        else:
            query = query.where(and_(FacturaModel.fecha_emision >= start_date, FacturaModel.fecha_emision <= end_date))

    # Filtros Opcionales Directos
    if router_id: query = query.where(ClienteModel.router_id == router_id)
    if cliente_id: query = query.where(FacturaModel.cliente_id == cliente_id)
    
    # Filtro de Búsqueda por Texto
    if busqueda:
        termino = f"%{busqueda.lower()}%"
        query = query.where(
            or_(
                func.lower(ClienteModel.nombre).like(termino),
                func.lower(ClienteModel.cedula).like(termino)
            )
        )
    
    # Filtro de Estado Inteligente
    today = date.today()
    if estado != "cualquiera":
        if estado == "adeudos":
            query = query.where(FacturaModel.estado.in_(["pendiente", "vencida"]))
        elif estado == "pendiente":
            query = query.where(FacturaModel.estado == "pendiente")
        elif estado == "vencida":
            query = query.where(FacturaModel.estado == "vencida")
        elif estado == "promesa":
            query = query.where(FacturaModel.es_promesa_activa == True)
        else:
            query = query.where(FacturaModel.estado == estado)

    # Ejecutar consulta
    result = await db.execute(query.order_by(desc(FacturaModel.id)))
    facturas = result.scalars().all()

    # Resumen y Formateo
    resumen = {
        "pagadas_cant": 0, "pagadas_total": 0.0, 
        "pendientes_cant": 0, "pendientes_total": 0.0, 
        "vencidas_cant": 0, "vencidas_total": 0.0, 
        "anuladas_cant": 0, "anuladas_total": 0.0
    }
    items_response = []

    for f in facturas:
        valor = float(f.total) if f.total else 0.0
        
        if f.estado == "pagada":
            resumen["pagadas_cant"] += 1
            resumen["pagadas_total"] += valor
        elif f.estado == "anulada":
            resumen["anuladas_cant"] += 1
            resumen["anuladas_total"] += valor
        elif f.estado == "vencida":
            resumen["vencidas_cant"] += 1
            resumen["vencidas_total"] += valor
        elif f.estado == "pendiente":
            fecha_limite = f.fecha_promesa_pago if (f.es_promesa_activa and f.fecha_promesa_pago) else f.fecha_vencimiento
            if fecha_limite and fecha_limite < today:
                resumen["vencidas_cant"] += 1
                resumen["vencidas_total"] += valor
            else:
                resumen["pendientes_cant"] += 1
                resumen["pendientes_total"] += valor
        
        items_response.append({
            "id": f.id,
            "estado": f.estado,
            "saldo_pendiente": f.saldo_pendiente,
            "tipo_factura": getattr(f, "tipo_factura", "mensual"),
            "concepto": getattr(f, "concepto", None),
            "descripcion": getattr(f, "descripcion", None),
            "afecta_corte": getattr(f, "afecta_corte", True),
            "creada_manual": getattr(f, "creada_manual", False),
            "total": f.total,
            "fecha_emision": f.fecha_emision,
            "fecha_vencimiento": f.fecha_vencimiento,
            "fecha_promesa_pago": f.fecha_promesa_pago, 
            "es_promesa_activa": f.es_promesa_activa,   
            "plan_snapshot": f.plan_snapshot,
            "cliente": {
                "id": f.cliente.id,
                "nombre": f.cliente.nombre,
                "ip_asignada": f.cliente.ip_asignada,
                "sn": f.cliente.cedula 
            }
        })

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
    except Exception as e:
        # Esto te dirá en el log si el error fue el PDF, el WhatsApp o la Base de Datos
        print(f"❌ ERROR EN COBRO: {str(e)}") 
        raise HTTPException(status_code=500, detail=str(e))


# ==========================================
# 3.5 FACTURA MANUAL / CARGO ADICIONAL
# ==========================================
@router.post("/factura-manual")
async def crear_factura_manual(
    data: FacturaManualRequest,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    cliente = await db.get(ClienteModel, data.cliente_id)
    if not cliente:
        raise HTTPException(404, "Cliente no encontrado")

    if cliente.estado == "eliminado":
        raise HTTPException(400, "No se puede crear factura a un cliente eliminado")

    if current_user.rol != "admin":
        allowed_router_ids = [r.id for r in current_user.routers_asignados]
        if cliente.router_id not in allowed_router_ids:
            raise HTTPException(403, "No tienes permiso para facturar este cliente")

    monto = round(float(data.monto or 0), 2)
    if monto <= 0:
        raise HTTPException(400, "El monto debe ser mayor a cero")

    concepto = data.concepto.strip()
    if not concepto:
        raise HTTPException(400, "El concepto es obligatorio")

    fecha_emision = date.today()
    fecha_vencimiento = data.fecha_vencimiento or fecha_emision

    res_servicio = await db.execute(
        select(ServicioModel)
        .where(ServicioModel.cliente_id == cliente.id)
        .order_by(ServicioModel.id.desc())
    )
    servicio = res_servicio.scalars().first()

    factura = FacturaModel(
        cliente_id=cliente.id,
        servicio_id=servicio.id if servicio else None,

        plan_snapshot="Cargo manual",
        detalles=data.descripcion or concepto,

        monto=monto,
        impuesto=0.0,
        total=monto,
        saldo_pendiente=monto,

        fecha_emision=fecha_emision,
        fecha_vencimiento=fecha_vencimiento,
        fecha_limite_corte=fecha_vencimiento if data.afecta_corte else None,

        mes_correspondiente=f"Cargo manual - {concepto}",
        estado="pendiente",

        periodo_desde=fecha_emision,
        periodo_hasta=fecha_emision,
        dias_facturados=1,
        dias_periodo=1,
        precio_mensual_snapshot=monto,
        precio_diario=monto,
        es_prorrateada=False,

        tipo_factura="manual",
        concepto=concepto,
        descripcion=data.descripcion,
        afecta_corte=data.afecta_corte,
        creada_manual=True,
    )

    db.add(factura)
    await db.commit()
    await db.refresh(factura)

    return {
        "status": "ok",
        "mensaje": "Factura manual creada correctamente",
        "factura": {
            "id": factura.id,
            "cliente_id": cliente.id,
            "cliente": cliente.nombre,
            "tipo_factura": factura.tipo_factura,
            "concepto": factura.concepto,
            "descripcion": factura.descripcion,
            "total": factura.total,
            "saldo_pendiente": factura.saldo_pendiente,
            "estado": factura.estado,
            "afecta_corte": factura.afecta_corte,
            "fecha_emision": factura.fecha_emision,
            "fecha_vencimiento": factura.fecha_vencimiento,
        },
    }


# 4. PROMESA DE PAGO
# ==========================================
@router.post("/promesa-pago")
async def registrar_promesa(
    data: PromesaPagoRequest,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    factura = await db.get(FacturaModel, data.factura_id)
    if not factura:
        raise HTTPException(404, "Factura no encontrada")

    cliente = await db.get(ClienteModel, factura.cliente_id)
    if not cliente:
        raise HTTPException(404, "Cliente no encontrado")

    if float(factura.saldo_pendiente or 0) <= 0:
        raise HTTPException(400, "La factura no tiene saldo pendiente")

    factura.fecha_promesa_pago = data.nueva_fecha
    factura.es_promesa_activa = True

    # La promesa no cambia el estado contable a "promesa".
    # La factura sigue cobrable como pendiente o vencida.
    if factura.fecha_vencimiento and factura.fecha_vencimiento < date.today():
        factura.estado = "vencida"
    elif factura.estado not in ["pendiente", "vencida"]:
        factura.estado = "pendiente"

    reactivado = False

    if cliente.estado == "suspendido":
        service = BillingService(db)
        reactivado = await service._reactivar_en_mikrotik(cliente)

        if reactivado:
            cliente.estado = "activo"
            await service._actualizar_estado_servicio_factura(
                factura,
                "activo",
            )
        
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
