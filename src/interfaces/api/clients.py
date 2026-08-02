from typing import List, Optional
from datetime import date
from decimal import Decimal
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
from src.infrastructure.auth import get_current_user, role_required
from src.infrastructure.models import (
    BajaServicioModel,
    ClienteModel,
    ConfiguracionModel,
    FacturaModel,
    MensajeChatModel,
)


# Servicios y Herramientas
from src.infrastructure.whatsapp_client import whatsapp_queue

from src.infrastructure.mikrotik_service import MikroTikService
from src.application.services.client_service import ClientService
from src.application.services.access_control_service import (
    filtro_clientes_del_tecnico,
    verificar_acceso_cliente,
)
from src.application.services.baja_service import (
    BajaService,
    ESTADOS_BAJA_ABIERTA,
)
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
    
    # 🚀 AÑADIDOS LOS IDs NECESARIOS PARA EL FRONTEND 🚀
    olt_id: Optional[int] = None
    onu_id: Optional[int] = None
    caja_nap_id: Optional[int] = None
    plan_id: Optional[int] = None       # Agregado por si el front lo necesita en el POST
    zona_id: Optional[int] = None
    
    router_nombre: str       
    estado: str              
    is_online: bool
    
    # Campos NAP y OLT 👇
    olt_nombre: Optional[str] = None
    nap_nombre: Optional[str] = None
    puerto_nap: Optional[int] = None

    router_id: Optional[int] = None 
    
    # Plan
    plan_nombre: str
    velocidad_bajada: int    
    velocidad_subida: int    
    precio_plan: Decimal
    
    # Financiero
    total_deuda: Decimal
    facturas_pendientes: int 
    fecha_corte: Optional[date] = None
    saldo_a_favor: Decimal

class EstadoUpdate(BaseModel):
    nuevo_estado: str

class MensajeManual(BaseModel):
    mensaje: str

class PromesaRequest(BaseModel):
    fecha_promesa: date


class CambioONURequest(BaseModel):
    nuevo_inventario_id: int
    estado_vieja_onu: str


# ==========================================
# 1. PORTAL TÉCNICO (QR - VISTA DETALLE Y ORDEN)
# ==========================================

@router.get("/{dato}/portal", response_model=ClientePortalResponse)
async def obtener_datos_portal(
    dato: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(
        role_required(["admin", "supervisor", "tecnico"])
    ),
):
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
    try:
        await verificar_acceso_cliente(db, current_user, cliente.id)
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error

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

    # --- D. DATOS EXTRA ---
    nap_nombre = cliente.caja_nap.nombre if cliente.caja_nap else "No Asignada"
    olt_nombre = cliente.olt.nombre if cliente.olt else "No Asignada"
    
    # Manejo seguro si no hay ONU asignada (para no enviar el string literal "Sin Equipo" que no coincidirá en el front)
    id_onu = cliente.onu_asignada.identificador if cliente.onu_asignada else None

    return {
        "id": cliente.id,
        "nombre": cliente.nombre,
        "cedula": cliente.cedula,
        "telefono": cliente.telefono,
        "direccion": cliente.direccion,
        "ip_asignada": cliente.ip_asignada or "Pendiente",
        
        # 🚀 ESTOS SON LOS IDs VITALES PARA EL FRONTEND DE REACT 🚀
        "olt_id": cliente.olt_id,
        "onu_id": cliente.onu_id,
        "caja_nap_id": cliente.caja_nap_id,
        "plan_id": cliente.plan_id,
        "router_id": cliente.router_id, 
        "zona_id": cliente.zona_id,
        
        # Datos visuales que ya tenías
        "identificador_onu": id_onu, 
        "router_nombre": cliente.router.nombre if cliente.router else "Sin Router",
        "estado": cliente.estado,
        "is_online": online_status,
        "olt_nombre": olt_nombre,
        "nap_nombre": nap_nombre,
        "puerto_nap": cliente.puerto_nap,
        "plan_nombre": cliente.plan.nombre if cliente.plan else "Sin Plan",
        "velocidad_bajada": cliente.plan.velocidad_bajada if cliente.plan else 0,
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
    db: AsyncSession = Depends(get_db),
    current_user=Depends(
        role_required(["admin", "supervisor", "cajero", "tecnico"])
    ),
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
                ClienteModel.cedula.ilike(filtro),       # 👈 Búsqueda limpia por Cédula
                ClienteModel.user_pppoe.ilike(filtro),   # ⚡ Extra: Puedes buscar por usuario PPPoE
                ClienteModel.ip_asignada.ilike(filtro)   # ⚡ Extra: Puedes buscar por IP asignada
            )
        )
        .group_by(ClienteModel.id)
        .limit(8)
    )
    if current_user.rol == "tecnico":
        stmt = stmt.where(
            filtro_clientes_del_tecnico(current_user.id)
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
    current_user=Depends(
        role_required(["admin", "supervisor", "tecnico"])
    ),
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
    if current_user.rol == "tecnico":
        query = query.where(
            filtro_clientes_del_tecnico(current_user.id)
        )
    elif tecnico_id:
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
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    service = ClientService(db)
    return await service.get_listado_unificado()

@router.get("/{cliente_id}", response_model=ClienteResponse)
async def obtener_cliente(
    cliente_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
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
    current_user = Depends(role_required(["admin", "supervisor"]))
):
    service = ClientService(db)
    try:
        return await service.registrar_cliente(
            cliente,
            background_tasks,
            usuario_operador=current_user,
        )
    except (ValueError, PermissionError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print(f"Error registro API: {e}")
        raise HTTPException(status_code=500, detail="Error interno al registrar cliente")


@router.post("/{cliente_id}/completar-instalacion")
async def completar_instalacion(
    cliente_id: int,
    datos: InstalacionRequest, 
    db: AsyncSession = Depends(get_db),
    current_user = Depends(role_required(["admin", "supervisor", "tecnico"]))
):
    service = ClientService(db)
    try:
        cliente_activado = await service.activar_instalacion(
            cliente_id,
            datos,
            usuario_operador=current_user,
        )
        
        return {
            "status": "success", 
            "message": "¡Servicio activado correctamente en el Router!", 
            "cliente": cliente_activado,
        }
    
    except PermissionError as error:
        await db.rollback()
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ValueError as ve:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(ve)) 
        
    except Exception as e:
        await db.rollback()
        print(f"Error activando: {e}")
        raise HTTPException(status_code=500, detail=f"Error en activación: {str(e)}")


@router.put("/{cliente_id}", response_model=ClienteResponse)
async def editar_cliente(
    cliente_id: int, 
    datos: ClienteCreate, 
    db: AsyncSession = Depends(get_db),
    current_user = Depends(role_required(["admin", "supervisor"]))
):
    service = ClientService(db)
    try:
        return await service.editar_cliente(cliente_id, datos)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


@router.delete("/{cliente_id}")
async def eliminar_cliente(
    cliente_id: int, 
    db: AsyncSession = Depends(get_db),
    current_user = Depends(role_required(["admin", "supervisor"]))
):
    service = ClientService(db)
    try:
        mensaje = await service.eliminar_cliente(cliente_id)
        return {"status": "ok", "message": mensaje}
    except ValueError as e:
        detalle = str(e)
        estado_http = 409 if "historial financiero u operativo" in detalle else 404
        raise HTTPException(status_code=estado_http, detail=detalle) from e


# ==========================================
# 4. ACCIONES ESPECÍFICAS (ESTADO, MENSAJES, CORTES)
# ==========================================

@router.put("/{cliente_id}/estado")
async def cambiar_estado(
    cliente_id: int, 
    estado: EstadoUpdate, 
    db: AsyncSession = Depends(get_db),
    current_user = Depends(role_required(["admin", "supervisor"]))
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
    current_user = Depends(role_required(["admin", "supervisor", "cajero"]))
):
    cliente = await db.get(ClienteModel, cliente_id)
    if not cliente or not cliente.telefono:
        raise HTTPException(status_code=404, detail="Cliente o teléfono no encontrado")
    
    try:
        registro = MensajeChatModel(
            cliente_id=cliente.id,
            telefono=whatsapp_queue.service._formatear_numero(
                cliente.telefono
            ),
            direccion="salida",
            mensaje=datos.mensaje,
            tipo_mensaje="texto",
            tipo_evento="mensaje_manual",
            leido=True,
            ack=0,
            estado_envio="pendiente",
            creado_por_id=current_user.id,
        )
        db.add(registro)
        await db.commit()
        await whatsapp_queue.agregar_tarea(
            {
                "mensaje_chat_id": registro.id,
                "intervalo": 2,
            }
        )

        return {
            "status": "encolado",
            "mensaje_id": registro.id,
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
    current_user = Depends(role_required(["admin", "supervisor", "cajero"]))
):
    service = ClientService(db)
    try:
        msg = await service.registrar_promesa_pago(
            cliente_id,
            datos.fecha_promesa,
            current_user.id,
        )
        return {"status": "ok", "message": msg}
    except (ValueError, PermissionError) as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/test/forzar-cortes-automaticos")
async def test_cortes_automaticos(
    db: AsyncSession = Depends(get_db),
    current_user = Depends(role_required(["admin"]))
):
    service = ClientService(db)
    try:
        cantidad = await service.procesar_suspensiones_automaticas()
        return {"status": "ok", "mensaje": "Proceso finalizado", "clientes_suspendidos_hoy": cantidad}
    except Exception as e:
        return {"status": "error", "detalle": str(e)}
    


@router.get("/{cliente_id}/diagnostico-fibra")
async def diagnostico_fibra_cliente(
    cliente_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(
        role_required(["admin", "supervisor", "tecnico"])
    ),
):
    """
    Botón mágico para el área de Soporte Técnico: 
    Revisa la potencia óptica en vivo de un solo cliente.
    """
    servicio = SNMPMonitorService(db)
    try:
        await verificar_acceso_cliente(db, current_user, cliente_id)
        resultado = await servicio.monitorear_cliente_individual(cliente_id)
        return {"status": "success", "data": resultado}
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error conectando con la OLT: {str(e)}")
    



@router.post("/{cliente_id}/dar-de-baja", deprecated=True)
async def dar_de_baja_cliente(
    cliente_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    """Compatibilidad: inicia una baja formal con retiro de equipo."""
    try:
        baja = await BajaService(db).crear(
            cliente_id=cliente_id,
            motivo="Baja solicitada desde el flujo anterior",
            usuario=current_user,
        )
        return {
            "status": "success",
            "baja_id": baja.id,
            "estado": baja.estado,
            "message": "Servicio cancelado y baja registrada",
        }
    except (ValueError, PermissionError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    

@router.post("/{cliente_id}/reactivar", deprecated=True)
async def reactivar_cliente_cancelado(
    cliente_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    """Reconecta a un cliente cancelado y lo vuelve a activar en MikroTik."""
    try:
        baja = (
            await db.execute(
                select(BajaServicioModel)
                .where(
                    BajaServicioModel.cliente_id == cliente_id,
                    BajaServicioModel.estado.in_(ESTADOS_BAJA_ABIERTA),
                )
                .order_by(BajaServicioModel.id.desc())
            )
        ).scalars().first()
        if not baja:
            raise ValueError(
                "No existe una baja reversible; crea una nueva instalación"
            )
        baja = await BajaService(db).cancelar_y_reactivar(
            baja.id,
            current_user,
        )
        return {
            "status": "success",
            "baja_id": baja.id,
            "message": "Baja cancelada y servicio reactivado",
        }
    except (ValueError, PermissionError) as e:
        raise HTTPException(status_code=400, detail=str(e))



# 🔥 CORRECCIÓN APLICADA AQUÍ: Usa inventario_id en lugar de cliente_id
@router.post(
    "/inventario/{inventario_id}/confirmar-retiro-onu",
    deprecated=True,
)
async def confirmar_retiro_onu(
    inventario_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor", "tecnico"])),
):
    """El técnico presiona esto al recuperar el equipo físico."""
    try:
        baja = (
            await db.execute(
                select(BajaServicioModel)
                .where(
                    BajaServicioModel.onu_id == inventario_id,
                    BajaServicioModel.estado == "pendiente_retiro",
                )
                .order_by(BajaServicioModel.id.desc())
            )
        ).scalars().first()
        if not baja:
            raise ValueError("No existe una baja abierta para esta ONU")
        resultado = await BajaService(db).confirmar_retiro(
            baja.id,
            "funcional",
            current_user,
            "Confirmado desde el endpoint compatible",
        )
        return {
            "status": "success",
            "baja_id": resultado.id,
            "message": "Retiro confirmado y equipo ingresado a bodega",
        }
    except (ValueError, PermissionError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    
@router.post(
    "/inventario/{inventario_id}/asignar-retiro/{tecnico_id}",
    deprecated=True,
)
async def asignar_retiro(
    inventario_id: int,
    tecnico_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    try:
        baja = (
            await db.execute(
                select(BajaServicioModel)
                .where(
                    BajaServicioModel.onu_id == inventario_id,
                    BajaServicioModel.estado == "pendiente_retiro",
                )
                .order_by(BajaServicioModel.id.desc())
            )
        ).scalars().first()
        if not baja:
            raise ValueError("No existe una baja abierta para esta ONU")
        resultado = await BajaService(db).asignar_tecnico(
            baja.id,
            tecnico_id,
            current_user,
        )
        return {
            "status": "success",
            "baja_id": resultado.id,
            "message": f"Retiro asignado al técnico ID: {tecnico_id}",
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

# 🔥 NUEVO ENDPOINT PARA EL CAMBIO POR FALLA 🔥
@router.post("/{cliente_id}/cambiar-onu")
async def cambiar_onu_cliente(
    cliente_id: int,
    req: CambioONURequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor", "tecnico"])),
):
    """Sustituye la ONU de un cliente y actualiza el inventario."""
    service = ClientService(db)
    try:
        await verificar_acceso_cliente(db, current_user, cliente_id)
        mensaje = await service.procesar_cambio_onu(
            cliente_id,
            req.nuevo_inventario_id,
            req.estado_vieja_onu,
            usuario_operador=current_user,
        )
        return {"status": "success", "message": mensaje}
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# FACTURACION_ISP_V2_RESUMEN_COMERCIAL_ENDPOINT
@router.get("/{cliente_id}/resumen-comercial")
async def obtener_resumen_comercial_cliente(
    cliente_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(role_required(["admin", "supervisor"])),
):
    """Resumen comercial para el detalle del cliente.

    Complementa /clientes/{id} con servicio vigente, factura actual,
    fechas de corte y deuda.
    """
    from datetime import date, datetime
    from decimal import Decimal
    from sqlalchemy import text

    def serializar(valor):
        if isinstance(valor, (date, datetime)):
            return valor.isoformat()
        if isinstance(valor, Decimal):
            return float(valor)
        return valor

    def fila_a_dict(fila):
        if not fila:
            return None
        return {
            key: serializar(value)
            for key, value in dict(fila._mapping).items()
        }

    servicio_sql = text("""
        SELECT
            s.id,
            s.cliente_id,
            s.plan_id,
            s.plantilla_id,
            s.estado,
            s.tipo_facturacion,
            s.ciclo_facturacion,
            s.fecha_instalacion,
            s.fecha_activacion,
            s.fecha_inicio_servicio,
            s.fecha_fin_periodo_gratis,
            s.fecha_inicio_cobro,
            s.proxima_facturacion,
            s.dia_vencimiento,
            s.dias_tolerancia,
            s.meses_gratis,
            s.politica_prorrateo,
            p.nombre AS plan_nombre,
            p.precio AS plan_precio,
            pf.nombre AS plantilla_nombre,
            pf.dia_pago,
            pf.dias_antes_emision,
            pf.dias_tolerancia AS plantilla_dias_tolerancia,
            pf.impuesto AS plantilla_impuesto
        FROM servicios s
        LEFT JOIN planes p ON p.id = s.plan_id
        LEFT JOIN plantillas_facturacion pf ON pf.id = s.plantilla_id
        WHERE s.cliente_id = :cliente_id
        ORDER BY
            CASE
                WHEN s.estado = 'activo' THEN 0
                WHEN s.estado = 'suspendido' THEN 1
                ELSE 2
            END,
            s.id DESC
        LIMIT 1
    """)

    factura_sql = text("""
        SELECT
            f.id,
            f.cliente_id,
            f.servicio_id,
            f.periodo_desde,
            f.periodo_hasta,
            f.dias_facturados,
            f.dias_periodo,
            f.total,
            f.saldo_pendiente,
            f.estado,
            f.es_prorrateada,
            f.tipo_facturacion_snapshot,
            f.ciclo_facturacion_snapshot,
            f.mes_correspondiente,
            f.fecha_emision,
            f.fecha_vencimiento,
            f.fecha_limite_corte
        FROM facturas f
        WHERE f.cliente_id = :cliente_id
        ORDER BY
            CASE
                WHEN f.estado IN ('pendiente', 'vencida') THEN 0
                ELSE 1
            END,
            CASE
                WHEN f.estado IN ('pendiente', 'vencida')
                THEN f.fecha_vencimiento
            END ASC,
            CASE
                WHEN f.estado NOT IN ('pendiente', 'vencida')
                THEN f.fecha_vencimiento
            END DESC,
            f.id DESC
        LIMIT 1
    """)

    resumen_sql = text("""
        SELECT
            COALESCE(COUNT(f.id), 0) AS facturas_abiertas,
            COALESCE(SUM(f.saldo_pendiente), 0) AS saldo_pendiente_total,
            MIN(f.fecha_vencimiento) AS proximo_vencimiento,
            MIN(f.fecha_limite_corte) AS proximo_corte
        FROM facturas f
        WHERE f.cliente_id = :cliente_id
          AND f.estado IN ('pendiente', 'vencida')
          AND f.saldo_pendiente > 0
    """)

    servicio = (await db.execute(servicio_sql, {"cliente_id": cliente_id})).first()
    factura = (await db.execute(factura_sql, {"cliente_id": cliente_id})).first()
    resumen = (await db.execute(resumen_sql, {"cliente_id": cliente_id})).first()

    return {
        "cliente_id": cliente_id,
        "servicio_actual": fila_a_dict(servicio),
        "factura_actual": fila_a_dict(factura),
        "resumen_deuda": fila_a_dict(resumen),
    }
