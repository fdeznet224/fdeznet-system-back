from typing import List, Optional
from datetime import date
import re 

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Body, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_ 
from sqlalchemy.orm import selectinload
from pydantic import BaseModel
from src.domain.schemas import InstalacionRequest

# Infraestructura y Base de Datos
from src.infrastructure.database import get_db
from src.infrastructure.auth import get_current_user
from src.infrastructure.models import ClienteModel, ConfiguracionModel

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
    router_nombre: str       
    estado: str              
    is_online: bool
    
    # Campos NAP
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
    """
    Busca por ID (si es numérico) o por Cédula (S/N).
    Usado por la App del Técnico para ver detalles antes y después de activar.
    """
    if dato.isdigit():
        criterio = ClienteModel.id == int(dato)
    else:
        criterio = ClienteModel.cedula == dato

    stmt = select(ClienteModel).options(
        selectinload(ClienteModel.plan),
        selectinload(ClienteModel.router),
        selectinload(ClienteModel.facturas),
        selectinload(ClienteModel.caja_nap)
    ).where(criterio) 
    
    res = await db.execute(stmt)
    cliente = res.scalar_one_or_none()
    
    if not cliente:
        raise HTTPException(status_code=404, detail="Cliente no encontrado")

    # --- A. CÁLCULOS FINANCIEROS ---
    facturas_pendientes = [f for f in cliente.facturas if f.estado == 'pendiente']
    total_deuda = sum(f.saldo_pendiente for f in facturas_pendientes)
    fecha_corte = None
    if facturas_pendientes:
        facturas_pendientes.sort(key=lambda x: x.fecha_vencimiento)
        fecha_corte = facturas_pendientes[0].fecha_vencimiento

    # --- B. DIAGNÓSTICO TÉCNICO (Ping) ---
    online_status = False
    # Solo intentamos ping si el cliente está activo y tiene IP real
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
    # Priorizamos lo que ya está en BD. Si no hay (orden nueva), sugerimos según formato.
    sug_user = cliente.user_pppoe if cliente.user_pppoe else RefPPP.generar_formato(cliente.nombre)
    sug_pass = cliente.pass_pppoe if cliente.pass_pppoe else "123456"

    # --- D. DATOS EXTRA ---
    nap_nombre = cliente.caja_nap.nombre if cliente.caja_nap else "No Asignada"

    return {
        "id": cliente.id,
        "nombre": cliente.nombre,
        "cedula": cliente.cedula,
        "telefono": cliente.telefono,
        "direccion": cliente.direccion,
        
        "ip_asignada": cliente.ip_asignada or "Pendiente",
        "mac_address": cliente.mac_address,
        "router_nombre": cliente.router.nombre if cliente.router else "Sin Router",
        "router_id": cliente.router_id, 
        "estado": cliente.estado,
        "is_online": online_status,
        
        "nap_nombre": nap_nombre,
        "puerto_nap": cliente.puerto_nap,

        "suggested_user": sug_user,
        "suggested_pass": sug_pass,
        
        "plan_nombre": cliente.plan.nombre if cliente.plan else "Sin Plan",
        "velocidad_bajada": cliente.plan.velocidad_bajada if cliente.plan else 0,
        "velocidad_subida": cliente.plan.velocidad_subida if cliente.plan else 0,
        "precio_plan": cliente.plan.precio if cliente.plan else 0.0,

        "total_deuda": total_deuda,
        "facturas_pendientes": len(facturas_pendientes),
        "fecha_corte": fecha_corte,
        "saldo_a_favor": cliente.saldo_a_favor or 0.0
    }


# ==========================================
# 2. GESTIÓN DE CLIENTES (CRUD & BÚSQUEDA)
# ==========================================

@router.get("/", response_model=List[ClienteResponse])
async def listar_clientes(
    router_id: Optional[int] = None, 
    search: Optional[str] = Query(None, description="Buscar por Nombre, SN/Cédula o IP"),
    tecnico_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    query = select(ClienteModel).options(
        selectinload(ClienteModel.plan),
        selectinload(ClienteModel.router),
        selectinload(ClienteModel.plantilla),
        selectinload(ClienteModel.zona),
        selectinload(ClienteModel.caja_nap),
        selectinload(ClienteModel.tecnico)
    )

    if search:
        filtro = f"%{search}%"
        query = query.where(
            or_(
                ClienteModel.nombre.ilike(filtro),     
                ClienteModel.cedula.ilike(filtro),     
                ClienteModel.ip_asignada.ilike(filtro) 
            )
        )
    
    if router_id:
        query = query.where(ClienteModel.router_id == router_id)

    if tecnico_id:
        query = query.where(
            ClienteModel.tecnico_id == tecnico_id,
            ClienteModel.estado != 'cancelado' 
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
        selectinload(ClienteModel.tecnico)
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
    """
    Paso 1: Crear Orden / Registrar Prospecto.
    Guarda en BD como 'pendiente_instalacion'. NO toca Mikrotik.
    """
    service = ClientService(db)
    try:
        return await service.registrar_cliente(cliente, background_tasks)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print(f"Error registro: {e}")
        raise HTTPException(status_code=500, detail="Error interno al registrar cliente")


@router.post("/{cliente_id}/completar-instalacion")
async def completar_instalacion(
    cliente_id: int,
    datos: InstalacionRequest, # 👈 AQUÍ PONEMOS EL BLINDAJE
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    service = ClientService(db)
    try:
        # Ahora le pasamos un objeto validado, no un diccionario
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
    # 1. Obtener al cliente para tener su número
    cliente = await db.get(ClienteModel, cliente_id)
    if not cliente or not cliente.telefono:
        raise HTTPException(status_code=404, detail="Cliente o teléfono no encontrado")
    
    try:
        # 2. 🚀 ENCOLAR PARA ENVÍO
        # Enviamos directo a la fila de WhatsApp sin guardar nada más
        await whatsapp_queue.agregar_tarea({
            "numero": cliente.telefono,
            "mensaje": datos.mensaje,
            "intervalo": 2  # Pausa de seguridad
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