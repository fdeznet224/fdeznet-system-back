from typing import List, Optional
from datetime import date
import re 
from sqlalchemy import select, or_, func

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Body, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from pydantic import BaseModel
from src.application.services.snmp_service import SNMPMonitorService
from src.domain.schemas import InstalacionRequest

# Infraestructura y Base de Datos
from src.infrastructure.database import get_db
from src.infrastructure.auth import get_current_user
from src.infrastructure.models import ClienteModel, ConfiguracionModel, FacturaModel


# Servicios y Herramientas
from src.infrastructure.whatsapp_client import whatsapp_queue

from src.infrastructure.mikrotik_service import MikroTikService
from src.application.services.client_service import ClientService
from src.infrastructure.repositories import ClienteRepository
from src.infrastructure import RefPPP 

# Schemas Globales
from src.domain.schemas import (
    ClienteCreate, 
    ClienteResponse, 
    ClienteFullResponse
)

router = APIRouter(prefix="/clientes", tags=["Gestión de Clientes"])

# ==========================================
# 0. SCHEMAS LOCALES (Auxiliares para respuestas específicas)
# ==========================================

class ClientePortalResponse(BaseModel):
    id: int
    nombre: str
    cedula: Optional[str] = None
    telefono: Optional[str] = None
    direccion: Optional[str] = None
    
    # Técnicos
    ip_asignada: Optional[str] = None
    mac_address: Optional[str] = None
    identificador_onu: Optional[str] = None # 👇 AÑADIDO PARA LA APP DEL TÉCNICO
    router_nombre: str       
    estado: str              
    is_online: bool
    
    # Campos NAP y OLT 👇
    olt_nombre: Optional[str] = None
    nap_nombre: Optional[str] = None
    puerto_nap: Optional[int] = None

    # Sugerencias PPPoE
    suggested_user: Optional[str] = None
    suggested_pass: Optional[str] = None
    router_id: Optional[int] = None 
    
    # Plan
    plan_nombre: str
    velocidad_bajada: int    
    velocidad_subida: int    
    precio_plan: float
    
    # Financiero
    total_deuda: float
    facturas_pendientes: int 
    fecha_corte: Optional[date] = None
    saldo_a_favor: float

class EstadoUpdate(BaseModel):
    nuevo_estado: str

class MensajeManual(BaseModel):
    mensaje: str

class PromesaRequest(BaseModel):
    fecha_promesa: date


# ==========================================
# 1. PORTAL TÉCNICO (QR - VISTA DETALLE Y ORDEN)
# ==========================================

@router.get("/{dato}/portal", response_model=ClientePortalResponse)
async def obtener_datos_portal(dato: str, db: AsyncSession = Depends(get_db)):
    if dato.isdigit():
        criterio = ClienteModel.id == int(dato)
    else:
        criterio = ClienteModel.cedula == dato

    stmt = select(ClienteModel).options(
        selectinload(ClienteModel.plan),
        selectinload(ClienteModel.router),
        selectinload(ClienteModel.facturas),
        selectinload(ClienteModel.caja_nap),
        selectinload(ClienteModel.olt),
        selectinload(ClienteModel.onu_asignada) # Relación de inventario corregida
    ).where(criterio) 
    
    res = await db.execute(stmt)
    cliente = res.scalar_one_or_none()
    
    if not cliente:
        raise HTTPException(status_code=404, detail="Cliente no encontrado")

    # --- A. CÁLCULOS FINANCIEROS (Sincronizado con listado-completo) ---
    # Filtramos facturas: pendientes, vencidas o promesas (Adeudos reales)
    facturas_adeudo = [f for f in cliente.facturas if f.estado in ['pendiente', 'vencida', 'promesa']]
    
    total_deuda = sum(f.saldo_pendiente for f in facturas_adeudo)
    
    # Contamos solo las que están realmente vencidas para la alerta roja
    vencidas_count = len([f for f in facturas_adeudo if f.estado == 'vencida'])
    
    fecha_corte = None
    if facturas_adeudo:
        # Ordenamos por vencimiento para mostrar la fecha más urgente
        facturas_adeudo.sort(key=lambda x: x.fecha_vencimiento)
        fecha_corte = facturas_adeudo[0].fecha_vencimiento

    # --- B. DIAGNÓSTICO TÉCNICO (Ping) ---
    online_status = False
    if cliente.estado == 'activo' and cliente.router and cliente.ip_asignada and cliente.ip_asignada != '0.0.0.0':
        try:
            mk = MikroTikService(
                ip=cliente.router.ip_vpn,
                user=cliente.router.user_api,
                password=cliente.router.pass_api,
                port=cliente.router.port_api
            )
            ping_res = mk.ping_desde_router(cliente.ip_asignada, count=1)
            if ping_res and ping_res.get("status") == "online":
                 online_status = True
        except Exception:
            online_status = False

    # --- C. CREDENCIALES PPPoE ---
    sug_user = cliente.user_pppoe if cliente.user_pppoe else f"user_{cliente.id}"
    sug_pass = cliente.pass_pppoe if cliente.pass_pppoe else "123456"

    # --- D. DATOS EXTRA ---
    nap_nombre = cliente.caja_nap.nombre if cliente.caja_nap else "No Asignada"
    olt_nombre = cliente.olt.nombre if cliente.olt else "No Asignada"
    id_onu = cliente.onu_asignada.identificador if cliente.onu_asignada else "Sin Equipo"

    return {
        "id": cliente.id,
        "nombre": cliente.nombre,
        "cedula": cliente.cedula,
        "telefono": cliente.telefono,
        "direccion": cliente.direccion,
        "ip_asignada": cliente.ip_asignada or "Pendiente",
        "identificador_onu": id_onu, 
        "router_nombre": cliente.router.nombre if cliente.router else "Sin Router",
        "router_id": cliente.router_id, 
        "estado": cliente.estado,
        "is_online": online_status,
        "olt_nombre": olt_nombre,
        "nap_nombre": nap_nombre,
        "puerto_nap": cliente.puerto_nap,
        "suggested_user": sug_user,
        "suggested_pass": sug_pass,
        "plan_nombre": cliente.plan.nombre if cliente.plan else "Sin Plan",
        "velocidad_bajada": cliente.plan.velocidad_bajada if cliente.plan else 0,
        
        # 🚀 CORRECCIÓN: Agregamos el campo faltante requerido por el esquema
        "velocidad_subida": cliente.plan.velocidad_subida if cliente.plan else 0,
        
        "precio_plan": cliente.plan.precio if cliente.plan else 0.0,
        "total_deuda": total_deuda,
        "facturas_pendientes": vencidas_count,
        "fecha_corte": fecha_corte,
        "saldo_a_favor": cliente.saldo_a_favor or 0.0
    }


# ==========================================
# 2. GESTIÓN DE CLIENTES (CRUD & BÚSQUEDA)
# ==========================================


@router.get("/buscar")
async def buscar_clientes_global(
    query: str = Query(..., min_length=3), 
    db: AsyncSession = Depends(get_db)
):
    filtro = f"%{query}%"
    stmt = (
        select(
            ClienteModel.id,
            ClienteModel.nombre,
            ClienteModel.telefono,
            ClienteModel.estado,
            ClienteModel.cedula,  
            func.coalesce(func.sum(FacturaModel.saldo_pendiente), 0).label("total_deuda")
        )
        .outerjoin(FacturaModel, (FacturaModel.cliente_id == ClienteModel.id) & (FacturaModel.estado == 'pendiente'))
        .where(
            or_(
                ClienteModel.nombre.ilike(filtro),
                ClienteModel.telefono.ilike(filtro),
                ClienteModel.cedula.ilike(filtro),     
                ClienteModel.identificador_onu.ilike(filtro) # 👇 BÚSQUEDA POR MAC O SERIAL
            )
        )
        .group_by(ClienteModel.id)
        .limit(8)
    )

    result = await db.execute(stmt)
    rows = result.mappings().all()
    
    return [
        {
            "id": r.id,
            "nombre": r.nombre,
            "cedula": r.cedula, 
            "telefono": r.telefono,
            "estado": r.estado,
            "total_deuda": float(r.total_deuda)
        }
        for r in rows
    ]

@router.get("/", response_model=List[ClienteResponse])
async def listar_clientes(
    router_id: Optional[int] = None, 
    search: Optional[str] = Query(None, description="Buscar por Nombre, SN/Cédula o IP"),
    tecnico_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    # 👇 1. Importación local de Inventario para la consulta cruzada
    from src.infrastructure.models import InventarioONUModel 

    # 👇 2. Agregamos 'onu_asignada' a las opciones de carga (SELECTINLOAD)
    query = select(ClienteModel).options(
        selectinload(ClienteModel.plan),
        selectinload(ClienteModel.router),
        selectinload(ClienteModel.plantilla),
        selectinload(ClienteModel.zona),
        selectinload(ClienteModel.caja_nap),
        selectinload(ClienteModel.tecnico),
        selectinload(ClienteModel.olt),
        selectinload(ClienteModel.onu_asignada) # 🔥 ESTO PERMITE QUE EL TECH VEA EL SERIAL
    )

    # 3. Filtro de búsqueda (Buscador general)
    if search:
        filtro = f"%{search}%"
        query = query.where(
            or_(
                ClienteModel.nombre.ilike(filtro),     
                ClienteModel.cedula.ilike(filtro),     
                ClienteModel.ip_asignada.ilike(filtro),
                # Buscamos también en el identificador de la ONU vinculada
                ClienteModel.onu_asignada.has(InventarioONUModel.identificador.ilike(filtro))
            )
        )
    
    if router_id:
        query = query.where(ClienteModel.router_id == router_id)

    # 👇 4. LÓGICA DE FILTRADO PARA EL TÉCNICO (CORE) 👇
    if tecnico_id:
        # El técnico debe ver:
        # A) Clientes donde él es el instalador Y no están cancelados.
        # B) Clientes cuya ONU en bodega tiene asignado su ID para retiro.
        query = query.where(
            or_(
                # Caso A: Instalaciones pendientes
                (ClienteModel.tecnico_id == tecnico_id) & (ClienteModel.estado != 'cancelado'),
                
                # Caso B: Retiros de equipos (Bajas) asignados a él en inventario
                ClienteModel.onu_asignada.has(InventarioONUModel.tecnico_id == tecnico_id)
            )
        )

    query = query.order_by(ClienteModel.id.desc()).limit(50)
    result = await db.execute(query)
    return result.scalars().all()


@router.get("/listado-completo-unificado", response_model=List[ClienteFullResponse])
async def get_clientes_unificados(
    db: AsyncSession = Depends(get_db), 
    current_user = Depends(get_current_user)
):
    service = ClientService(db)
    return await service.get_listado_unificado()

@router.get("/{cliente_id}", response_model=ClienteResponse)
async def obtener_cliente(cliente_id: int, db: AsyncSession = Depends(get_db)):
    query = select(ClienteModel).options(
        selectinload(ClienteModel.plan),
        selectinload(ClienteModel.router),
        selectinload(ClienteModel.plantilla),
        selectinload(ClienteModel.zona),
        selectinload(ClienteModel.caja_nap),
        selectinload(ClienteModel.tecnico),
        selectinload(ClienteModel.olt),
        selectinload(ClienteModel.onu_asignada)
    ).where(ClienteModel.id == cliente_id)
    
    result = await db.execute(query)
    cliente = result.scalar_one_or_none()

    if not cliente:
        raise HTTPException(status_code=404, detail="Cliente no encontrado")
    
    return cliente


# ==========================================
# 3. CREACIÓN Y ACTIVACIÓN (FLUJO HÍBRIDO) 🚀
# ==========================================

@router.post("/", response_model=ClienteResponse)
async def registrar_cliente(
    cliente: ClienteCreate, 
    background_tasks: BackgroundTasks, 
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    service = ClientService(db)
    try:
        return await service.registrar_cliente(cliente, background_tasks)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print(f"Error registro API: {e}")
        raise HTTPException(status_code=500, detail="Error interno al registrar cliente")


@router.post("/{cliente_id}/completar-instalacion")
async def completar_instalacion(
    cliente_id: int,
    datos: InstalacionRequest, 
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    service = ClientService(db)
    try:
        cliente_activado = await service.activar_instalacion(cliente_id, datos)
        
        return {
            "status": "success", 
            "message": "¡Servicio activado correctamente en el Router!", 
            "cliente": cliente_activado.nombre
        }
    
    except ValueError as ve:
        raise HTTPException(status_code=409, detail=str(ve)) 
        
    except Exception as e:
        print(f"Error activando: {e}")
        raise HTTPException(status_code=500, detail=f"Error en activación: {str(e)}")


@router.put("/{cliente_id}", response_model=ClienteResponse)
async def editar_cliente(
    cliente_id: int, 
    datos: ClienteCreate, 
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    service = ClientService(db)
    try:
        return await service.editar_cliente(cliente_id, datos)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/{cliente_id}")
async def eliminar_cliente(
    cliente_id: int, 
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    service = ClientService(db)
    try:
        mensaje = await service.eliminar_cliente(cliente_id)
        return {"status": "ok", "message": mensaje}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ==========================================
# 4. ACCIONES ESPECÍFICAS (ESTADO, MENSAJES, CORTES)
# ==========================================

@router.put("/{cliente_id}/estado")
async def cambiar_estado(
    cliente_id: int, 
    estado: EstadoUpdate, 
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    service = ClientService(db)
    try:
        msg = await service.cambiar_estado(cliente_id, estado.nuevo_estado)
        return {"status": "ok", "message": msg}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

@router.post("/{cliente_id}/mensaje")
async def enviar_mensaje_directo(
    cliente_id: int, 
    datos: MensajeManual, 
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    cliente = await db.get(ClienteModel, cliente_id)
    if not cliente or not cliente.telefono:
        raise HTTPException(status_code=404, detail="Cliente o teléfono no encontrado")
    
    try:
        await whatsapp_queue.agregar_tarea({
            "numero": cliente.telefono,
            "mensaje": datos.mensaje,
            "intervalo": 2  
        })

        return {
            "status": "enviando", 
            "cliente": cliente.nombre,
            "destino": cliente.telefono
        }

    except Exception as e:
        print(f"❌ Error al procesar envío manual: {e}")
        raise HTTPException(status_code=500, detail="Error al programar el mensaje")

@router.post("/{cliente_id}/promesa-pago")
async def crear_promesa_pago(
    cliente_id: int, 
    datos: PromesaRequest, 
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    service = ClientService(db)
    try:
        msg = await service.registrar_promesa_pago(cliente_id, datos.fecha_promesa)
        return {"status": "ok", "message": msg}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/test/forzar-cortes-automaticos")
async def test_cortes_automaticos(
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    service = ClientService(db)
    try:
        cantidad = await service.procesar_suspensiones_automaticas()
        return {"status": "ok", "mensaje": "Proceso finalizado", "clientes_suspendidos_hoy": cantidad}
    except Exception as e:
        return {"status": "error", "detalle": str(e)}
    


@router.get("/{cliente_id}/diagnostico-fibra")
async def diagnostico_fibra_cliente(cliente_id: int, db: AsyncSession = Depends(get_db)):
    """
    Botón mágico para el área de Soporte Técnico: 
    Revisa la potencia óptica en vivo de un solo cliente.
    """
    servicio = SNMPMonitorService(db)
    try:
        resultado = await servicio.monitorear_cliente_individual(cliente_id)
        return {"status": "success", "data": resultado}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error conectando con la OLT: {str(e)}")
    



@router.post("/{cliente_id}/dar-de-baja")
async def dar_de_baja_cliente(cliente_id: int, db: AsyncSession = Depends(get_db)):
    """Desconecta al cliente de MikroTik y envía la ONU a 'Por Recoger'"""
    service = ClientService(db)
    try:
        mensaje = await service.procesar_baja_servicio(cliente_id)
        return {"status": "success", "message": mensaje}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/{cliente_id}/confirmar-retiro-onu")
async def confirmar_retiro_onu(cliente_id: int, db: AsyncSession = Depends(get_db)):
    """El técnico presiona esto al recuperar el equipo. Libera la MAC/Serial."""
    service = ClientService(db)
    try:
        mensaje = await service.confirmar_retiro_tecnico(cliente_id)
        return {"status": "success", "message": mensaje}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    

@router.post("/{cliente_id}/asignar-retiro/{tecnico_id}") 
async def asignar_retiro(cliente_id: int, tecnico_id: int, db: AsyncSession = Depends(get_db)):
    service = ClientService(db)
    try:
        mensaje = await service.asignar_tecnico_retiro(cliente_id, tecnico_id)
        return {"status": "success", "message": mensaje}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))