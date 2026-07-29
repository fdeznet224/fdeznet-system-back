from decimal import Decimal
from typing import Optional
from datetime import date
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_, func, desc
from sqlalchemy.orm import joinedload, selectinload
from pydantic import BaseModel, Field

# Infraestructura
from src.infrastructure.database import get_db
from src.infrastructure.auth import get_current_user, role_required
from src.infrastructure.models import (
    FacturaModel, 
    ClienteModel, 
    PagoModel, 
    UsuarioModel,
    ServicioModel,
    PoliticaCobranzaModel,
    ZonaModel,
    RouterModel,
)

# Servicios
from src.application.services.billing_service import BillingService
from src.application.services.finance_service import FinanceService

router = APIRouter(prefix="/finanzas", tags=["Módulo Financiero"])

# ==========================================
# 0. SCHEMAS LOCALES (Input)
# ==========================================
class CobroFullRequest(BaseModel):
    factura_id: int
    metodo_pago: str  # efectivo, transferencia, etc.
    monto_recibido: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    referencia: Optional[str] = None
    clave_idempotencia: Optional[str] = Field(None, max_length=100)

class PromesaPagoRequest(BaseModel):
    factura_id: int
    nueva_fecha: date
    notas: Optional[str] = None


class FacturaManualRequest(BaseModel):
    cliente_id: int
    concepto: str
    monto: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    descripcion: Optional[str] = None
    fecha_vencimiento: Optional[date] = None
    afecta_corte: bool = False


class MotivoRequest(BaseModel):
    motivo: str = Field(min_length=5, max_length=500)


class DescuentoRequest(MotivoRequest):
    monto: Decimal = Field(gt=0, max_digits=12, decimal_places=2)


class PoliticaCobranzaRequest(BaseModel):
    nombre: str = Field(min_length=3, max_length=100)
    tipo_cliente: str = Field(min_length=3, max_length=30)
    dias_max_promesa: int = Field(ge=1, le=60)
    max_promesas_activas: int = Field(ge=0, le=10)
    max_incumplidas_90_dias: int = Field(ge=0, le=20)
    permite_reconexion: bool = True
    activa: bool = True


class AsignarPoliticaRequest(BaseModel):
    politica_id: int


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

    # 🔥 Solución al "Mes Fantasma" y filtro de pagos
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
async def generar_masivo(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin"])),
):
    """MODO AUTOMÁTICO (CRONJOB)"""
    service = BillingService(db)
    reporte = await service.generar_emision_masiva() 
    return {"mensaje": "Proceso automático finalizado", "detalles": reporte}

@router.post("/manual/generar-facturas/{dia_pago}")
async def generar_facturas_manual(
    dia_pago: int, 
    db: AsyncSession = Depends(get_db),
    current_user = Depends(role_required(["admin", "supervisor"]))
):
    """MODO MANUAL (FORZADO)"""
    service = BillingService(db)
    try:
        resultado = await service.generar_emision_masiva(dia_objetivo=dia_pago)
        return {"status": "ok", "mensaje": f"Proceso manual finalizado para el Grupo de Pago día {dia_pago}", "detalles": resultado}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/ejecutar-cortes-emergencia")
async def forzar_cortes_ahora(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin"])),
):
    """Botón de pánico para ejecutar cortes sin esperar al Cron"""
    service = BillingService(db)
    try:
        resultado = await service.procesar_cortes_automaticos()
        return {"status": "ok", "resultado": resultado}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==========================================
# 3. REGISTRAR COBRO (liquidación por período)
# ==========================================
@router.post("/cobrar")
async def registrar_cobro(
    data: CobroFullRequest,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(role_required(["admin", "supervisor", "cajero"]))
):
    service = BillingService(db)
    try:
        resultado = await service.registrar_pago_completo(
            factura_id=data.factura_id,
            usuario_operador=current_user,
            metodo_pago=data.metodo_pago,
            monto=data.monto_recibido,
            referencia=data.referencia,
            clave_idempotencia=data.clave_idempotencia,
        )
        return resultado
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/pagos/{pago_id}/anular")
async def anular_pago(
    pago_id: int,
    data: MotivoRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    try:
        pago, factura, cliente = await FinanceService(db).anular_pago(
            pago_id,
            current_user.id,
            data.motivo,
        )
        return {
            "status": "ok",
            "pago_id": pago.id,
            "estado": pago.estado,
            "factura_id": factura.id,
            "saldo_restaurado": factura.saldo_pendiente,
            "saldo_a_favor": cliente.saldo_a_favor,
        }
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/facturas/{factura_id}/aplicar-saldo-favor")
async def aplicar_saldo_favor(
    factura_id: int,
    data: DescuentoRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor", "cajero"])),
):
    try:
        resultado = await BillingService(db).registrar_pago_completo(
            factura_id=factura_id,
            usuario_operador=current_user,
            metodo_pago="saldo_favor",
            monto=data.monto,
            referencia=f"Saldo a favor: {data.motivo}",
        )
        return resultado
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/facturas/{factura_id}/descuento")
async def aplicar_descuento(
    factura_id: int,
    data: DescuentoRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    try:
        registro, factura = await FinanceService(db).aplicar_descuento(
            factura_id,
            data.monto,
            data.motivo,
            current_user.id,
        )
        return {
            "status": "ok",
            "descuento_id": registro.id,
            "factura_id": factura.id,
            "descuento_total": factura.descuento_total,
            "saldo_pendiente": factura.saldo_pendiente,
            "estado": factura.estado,
        }
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ==========================================
# 3.5 FACTURA MANUAL / CARGO ADICIONAL
# ==========================================
@router.post("/factura-manual")
async def crear_factura_manual(
    data: FacturaManualRequest,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(role_required(["admin", "supervisor", "cajero"]))
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

    monto = FinanceService.dinero(data.monto)

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
        impuesto=Decimal("0.00"),
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
    current_user = Depends(role_required(["admin", "supervisor", "cajero"]))
):
    try:
        promesa, factura, cliente, politica, reactivado = (
            await BillingService(db).registrar_promesa_y_reactivar(
                data.factura_id,
                data.nueva_fecha,
                current_user.id,
                data.notas,
            )
        )
        return {
            "status": "ok",
            "promesa_id": promesa.id,
            "mensaje": f"Promesa registrada hasta {data.nueva_fecha}",
            "politica": politica.nombre,
            "reactivado": reactivado,
        }
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/politicas-cobranza")
async def listar_politicas_cobranza(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor", "cajero"])),
):
    politicas = (
        await db.execute(
            select(PoliticaCobranzaModel).order_by(PoliticaCobranzaModel.nombre)
        )
    ).scalars().all()
    return [
        {
            "id": item.id,
            "nombre": item.nombre,
            "tipo_cliente": item.tipo_cliente,
            "dias_max_promesa": item.dias_max_promesa,
            "max_promesas_activas": item.max_promesas_activas,
            "max_incumplidas_90_dias": item.max_incumplidas_90_dias,
            "permite_reconexion": item.permite_reconexion,
            "activa": item.activa,
        }
        for item in politicas
    ]


@router.post("/politicas-cobranza")
async def crear_politica_cobranza(
    data: PoliticaCobranzaRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin"])),
):
    tipo = data.tipo_cliente.strip().lower()
    existente = (
        await db.execute(
            select(PoliticaCobranzaModel).where(
                or_(
                    PoliticaCobranzaModel.nombre == data.nombre.strip(),
                    PoliticaCobranzaModel.tipo_cliente == tipo,
                )
            )
        )
    ).scalar_one_or_none()
    if existente:
        raise HTTPException(
            status_code=409,
            detail="Ya existe una política con ese nombre o tipo de cliente",
        )
    politica = PoliticaCobranzaModel(
        nombre=data.nombre.strip(),
        tipo_cliente=tipo,
        dias_max_promesa=data.dias_max_promesa,
        max_promesas_activas=data.max_promesas_activas,
        max_incumplidas_90_dias=data.max_incumplidas_90_dias,
        permite_reconexion=data.permite_reconexion,
        activa=data.activa,
    )
    db.add(politica)
    await db.commit()
    await db.refresh(politica)
    return {"status": "ok", "politica_id": politica.id}


@router.put("/politicas-cobranza/{politica_id}")
async def actualizar_politica_cobranza(
    politica_id: int,
    data: PoliticaCobranzaRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin"])),
):
    politica = await db.get(PoliticaCobranzaModel, politica_id)
    if not politica:
        raise HTTPException(status_code=404, detail="Política no encontrada")
    politica.nombre = data.nombre.strip()
    politica.tipo_cliente = data.tipo_cliente.strip().lower()
    politica.dias_max_promesa = data.dias_max_promesa
    politica.max_promesas_activas = data.max_promesas_activas
    politica.max_incumplidas_90_dias = data.max_incumplidas_90_dias
    politica.permite_reconexion = data.permite_reconexion
    politica.activa = data.activa
    await db.commit()
    return {"status": "ok", "politica_id": politica.id}


@router.put("/clientes/{cliente_id}/politica-cobranza")
async def asignar_politica_cobranza(
    cliente_id: int,
    data: AsignarPoliticaRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    cliente = await db.get(ClienteModel, cliente_id)
    politica = await db.get(PoliticaCobranzaModel, data.politica_id)
    if not cliente or not politica:
        raise HTTPException(status_code=404, detail="Cliente o política no encontrada")
    if not politica.activa:
        raise HTTPException(status_code=400, detail="La política está inactiva")
    cliente.politica_cobranza_id = politica.id
    cliente.tipo_cliente = politica.tipo_cliente
    await db.commit()
    return {"status": "ok", "cliente_id": cliente.id, "politica_id": politica.id}


# ==========================================
# 5. REPORTE DE COBRANZA Y GRÁFICAS
# ==========================================
@router.get("/pagos-reporte")
async def obtener_reporte_cobranza(
    start_date: date,
    end_date: date,
    usuario_id: Optional[int] = Query(None),
    router_id: Optional[int] = Query(None),
    zona_id: Optional[int] = Query(None),
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
        UsuarioModel.nombre_completo.label("usuario_nombre"),
        ZonaModel.nombre.label("zona_nombre"),
        RouterModel.nombre.label("router_nombre"),
    ).join(ClienteModel, PagoModel.cliente_id == ClienteModel.id)\
     .outerjoin(UsuarioModel, PagoModel.usuario_id == UsuarioModel.id)\
     .outerjoin(ZonaModel, ClienteModel.zona_id == ZonaModel.id)\
     .outerjoin(RouterModel, ClienteModel.router_id == RouterModel.id)

    query = query.where(func.date(PagoModel.fecha_pago) >= start_date)
    query = query.where(func.date(PagoModel.fecha_pago) <= end_date)
    query = query.where(PagoModel.estado == "aplicado")

    if current_user.rol != 'admin':
        query = query.where(PagoModel.usuario_id == current_user.id)
    else:
        if usuario_id: query = query.where(PagoModel.usuario_id == usuario_id)

    if router_id:
        query = query.where(ClienteModel.router_id == router_id)
    if zona_id:
        query = query.where(ClienteModel.zona_id == zona_id)

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
                "estado": "aplicado",
                "cliente_nombre": row.cliente_nombre,
                "usuario_nombre": row.usuario_nombre or "Sistema"
                ,"zona_nombre": row.zona_nombre or "Sin zona"
                ,"router_nombre": row.router_nombre or "Sin MikroTik"
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


@router.get("/cobranza/pendientes-diarios")
async def pendientes_diarios(
    fecha: Optional[date] = None,
    zona_id: Optional[int] = None,
    router_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor", "cajero"])),
):
    fecha = fecha or date.today()
    query = (
        select(FacturaModel)
        .join(ClienteModel)
        .options(joinedload(FacturaModel.cliente))
        .where(
            FacturaModel.estado.in_(["pendiente", "vencida"]),
            FacturaModel.saldo_pendiente > 0,
            or_(
                FacturaModel.fecha_vencimiento <= fecha,
                and_(
                    FacturaModel.es_promesa_activa.is_(True),
                    FacturaModel.fecha_promesa_pago <= fecha,
                ),
            ),
        )
    )
    if current_user.rol != "admin":
        permitidos = [router.id for router in current_user.routers_asignados]
        if not permitidos:
            return {"fecha": fecha, "total": Decimal("0.00"), "items": []}
        query = query.where(ClienteModel.router_id.in_(permitidos))
    if zona_id:
        query = query.where(ClienteModel.zona_id == zona_id)
    if router_id:
        query = query.where(ClienteModel.router_id == router_id)

    facturas = (
        await db.execute(
            query.order_by(
                FacturaModel.fecha_vencimiento.asc(),
                FacturaModel.saldo_pendiente.desc(),
            )
        )
    ).scalars().all()
    return {
        "fecha": fecha,
        "total": sum(
            (Decimal(item.saldo_pendiente or 0) for item in facturas),
            Decimal("0"),
        ),
        "items": [
            {
                "factura_id": item.id,
                "cliente_id": item.cliente_id,
                "cliente": item.cliente.nombre,
                "telefono": item.cliente.telefono,
                "direccion": item.cliente.direccion,
                "saldo_pendiente": item.saldo_pendiente,
                "fecha_vencimiento": item.fecha_vencimiento,
                "promesa_activa": item.es_promesa_activa,
                "fecha_promesa": item.fecha_promesa_pago,
            }
            for item in facturas
        ],
    }


@router.get("/resumen-operativo")
async def resumen_operativo(
    start_date: date,
    end_date: date,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    # Resumen de cobranza; no existe una caja física que conciliar.
    deuda = (
        await db.execute(
            select(func.coalesce(func.sum(FacturaModel.saldo_pendiente), 0)).where(
                FacturaModel.estado.in_(["pendiente", "vencida"])
            )
        )
    ).scalar_one()
    recuperado = (
        await db.execute(
            select(func.coalesce(func.sum(PagoModel.monto_aplicado), 0)).where(
                func.date(PagoModel.fecha_pago) >= start_date,
                func.date(PagoModel.fecha_pago) <= end_date,
                PagoModel.estado == "aplicado",
            )
        )
    ).scalar_one()
    return {
        "desde": start_date,
        "hasta": end_date,
        "ingresos": recuperado,
        "egresos": Decimal("0"),
        "neto": recuperado,
        "cartera_vencida_y_pendiente": deuda,
        "deuda_recuperada": recuperado,
    }
