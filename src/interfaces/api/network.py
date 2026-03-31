import pandas as pd
import ipaddress
import io
from typing import List
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse # 👈 CORRECCIÓN CLAVE: Usamos StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from src.application.services.vpn_service import VPNService 
from src.domain.schemas import WireguardConfigResponse

# Infraestructura y Auth
from src.infrastructure.database import get_db
from src.infrastructure.auth import role_required
from src.infrastructure.models import (
    RouterModel, 
    RedModel, 
    ClienteModel, 
    PlanModel
)

# Schemas
from src.domain.schemas import (
    RouterCreate, 
    RouterResponse, 
    RedCreate, 
    RedResponse,
    RouterUpdate
)

# Servicios
from src.infrastructure.mikrotik_service import MikroTikService
from src.application.services.network_service import NetworkService

router = APIRouter(prefix="/network", tags=["Infraestructura y Redes"])

# ==========================================
# 1. GESTIÓN DE ROUTERS
# ==========================================

@router.get("/routers/", response_model=List[RouterResponse])
async def listar_routers(db: AsyncSession = Depends(get_db)):
    """Lista todos los routers (Nodos/OLTs) registrados."""
    result = await db.execute(select(RouterModel)) 
    return result.scalars().all()

@router.post("/routers/", response_model=RouterResponse)
async def crear_router(
    router_data: RouterCreate, 
    db: AsyncSession = Depends(get_db),
    current_user = Depends(role_required(["admin"]))
):
    """Registra un nuevo router y verifica conexión básica."""
    service = NetworkService(db)
    try:
        return await service.crear_router(router_data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.put("/routers/{router_id}", response_model=RouterResponse)
async def editar_router(
    router_id: int, 
    router_data: RouterUpdate, # Usamos el nuevo Schema opcional
    db: AsyncSession = Depends(get_db),
    current_user = Depends(role_required(["admin"]))
):
    """Actualiza la configuración del Router de forma segura y validada."""
    
    # 1. Buscar el router actual en la DB
    router_db = await db.get(RouterModel, router_id)
    if not router_db:
        raise HTTPException(status_code=404, detail="Router no encontrado")
    
    # 2. Convertir datos de entrada a diccionario (solo los que el usuario envió)
    datos_nuevos = router_data.dict(exclude_unset=True)

    # 3. Si se intenta cambiar la IP, verificar que no esté duplicada
    if 'ip_vpn' in datos_nuevos and datos_nuevos['ip_vpn'] != router_db.ip_vpn:
        stmt = select(RouterModel).where(RouterModel.ip_vpn == datos_nuevos['ip_vpn'])
        res = await db.execute(stmt)
        if res.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="Esa IP ya está registrada en otro nodo.")

    # 4. PRUEBA DE CONEXIÓN ANTES DE GUARDAR (Filtro de seguridad)
    try:
        # Si no mandaron pass nueva, usamos la que ya tenemos en la DB
        pass_test = datos_nuevos.get('pass_api') if datos_nuevos.get('pass_api') else router_db.pass_api
        ip_test = datos_nuevos.get('ip_vpn', router_db.ip_vpn)
        user_test = datos_nuevos.get('user_api', router_db.user_api)
        port_test = datos_nuevos.get('port_api', router_db.port_api)

        mk = MikroTikService(ip_test, user_test, pass_test, port_test)
        conectado, msg = mk.probar_conexion()
        
        if not conectado:
            raise HTTPException(
                status_code=400, 
                detail=f"Los nuevos datos no permiten conexión con el MikroTik: {msg}"
            )
    except Exception as e:
        if isinstance(e, HTTPException): raise e
        raise HTTPException(status_code=400, detail=f"Error técnico al validar: {str(e)}")

    # 5. Aplicar los cambios al objeto de la DB
    for key, value in datos_nuevos.items():
        # Regla: No sobrescribir con vacío/None si es la contraseña
        if key == "pass_api" and (value == "" or value is None):
            continue
        setattr(router_db, key, value)
    
    try:
        await db.commit()
        await db.refresh(router_db)
        return router_db
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail="Error interno al guardar en base de datos")

@router.delete("/routers/{router_id}")
async def eliminar_router(
    router_id: int, 
    db: AsyncSession = Depends(get_db),
    current_user = Depends(role_required(["admin"]))
):
    """Elimina un router (Solo si no tiene clientes)."""
    router_db = await db.get(RouterModel, router_id)
    if not router_db:
        raise HTTPException(status_code=404, detail="Router no encontrado")

    # Seguridad: Bloquear si tiene clientes
    clientes = await db.execute(select(ClienteModel).where(ClienteModel.router_id == router_id).limit(1))
    if clientes.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="No se puede eliminar: Tiene clientes asociados.")

    await db.delete(router_db)
    await db.commit()
    
    return {"status": "success", "message": "Router eliminado correctamente"}

@router.get("/routers/{router_id}/ping")
async def verificar_estado_router(router_id: int, db: AsyncSession = Depends(get_db)):
    """Prueba de conexión simple."""
    r = await db.get(RouterModel, router_id)
    if not r: raise HTTPException(404, "Router no encontrado")
    
    try:
        mk = MikroTikService(r.ip_vpn, r.user_api, r.pass_api, r.port_api)
        conectado, msg = mk.probar_conexion()
        return {"status": "online" if conectado else "offline", "mensaje": msg}
    except Exception as e:
        return {"status": "offline", "mensaje": str(e)}

@router.post("/routers/{router_id}/sync")
async def sincronizar_configuracion_router(
    router_id: int, 
    db: AsyncSession = Depends(get_db),
    current_user = Depends(role_required(["admin"]))
):
    """Sincroniza planes y configuraciones al Mikrotik."""
    service = NetworkService(db)
    try:
        resultado = await service.sincronizar_router(router_id)
        return {"status": "success", "message": resultado}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error interno: {str(e)}")


# ==========================================
# 2. GESTIÓN DE REDES (IPAM)
# ==========================================

@router.get("/redes/", response_model=List[RedResponse])
async def listar_todas_las_redes(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(RedModel))
    return result.scalars().all()

@router.post("/redes/", response_model=RedResponse)
async def crear_red(
    red: RedCreate, 
    db: AsyncSession = Depends(get_db),
    current_user = Depends(role_required(["admin"]))
):
    service = NetworkService(db)
    try:
        return await service.crear_red(red)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.put("/redes/{red_id}", response_model=RedResponse)
async def editar_red(
    red_id: int, 
    red_data: RedCreate, 
    db: AsyncSession = Depends(get_db),
    current_user = Depends(role_required(["admin"]))
):
    """Actualiza CIDR, nombre o Gateway."""
    service = NetworkService(db)
    try:
        return await service.editar_red(red_id, red_data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.delete("/redes/{red_id}")
async def eliminar_red(
    red_id: int, 
    db: AsyncSession = Depends(get_db),
    current_user = Depends(role_required(["admin"]))
):
    """Elimina una red (Solo si está vacía)."""
    service = NetworkService(db)
    try:
        mensaje = await service.eliminar_red(red_id)
        return {"status": "success", "message": mensaje}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/redes/router/{router_id}", response_model=List[RedResponse])
async def listar_redes_por_router(router_id: int, db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(RedModel).where(RedModel.router_id == router_id))
    return res.scalars().all()

@router.get("/redes/{red_id}/ips-libres", response_model=List[str])
async def obtener_ips_libres(red_id: int, db: AsyncSession = Depends(get_db)):
    """Calcula las siguientes IPs disponibles."""
    red = await db.get(RedModel, red_id)
    if not red: raise HTTPException(404, "Red no encontrada")

    stmt = select(ClienteModel.ip_asignada).where(ClienteModel.ip_asignada.isnot(None))
    result = await db.execute(stmt)
    ips_ocupadas = {ip.strip() for ip in result.scalars().all() if ip and ip.strip()}
    
    if red.gateway: ips_ocupadas.add(red.gateway.strip())

    ips_disponibles = []
    try:
        network = ipaddress.ip_network(red.cidr, strict=False)
        for ip in network.hosts():
            ip_str = str(ip)
            if ip_str not in ips_ocupadas:
                ips_disponibles.append(ip_str)
                if len(ips_disponibles) >= 254: break
    except ValueError:
        raise HTTPException(400, "CIDR de red inválido")

    return ips_disponibles


# ==========================================
# 3. IMPORTACIÓN INTELIGENTE (EXCEL)
# ==========================================

@router.get("/importar/plantilla-inteligente")
async def descargar_plantilla_preparada(
    router_id: int,
    red_id: int,
    zona_id: int,
    plantilla_id: int,
    plan_id: int,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(role_required(["admin"]))
):
    """Genera y descarga el Excel directamente desde la memoria RAM (100% PPPoE)."""
    
    # 1. Obtener Datos
    router_db = await db.get(RouterModel, router_id)
    red_db = await db.get(RedModel, red_id)
    if not router_db or not red_db: raise HTTPException(404, "Datos no encontrados")

    planes_db = (await db.execute(select(PlanModel).where(PlanModel.router_id == router_id))).scalars().all()
    mapa_planes = {p.nombre.strip().lower(): p.id for p in planes_db if p.nombre}

    # 2. Leer Mikrotik (SOLO PPPoE)
    mk = MikroTikService(router_db.ip_vpn, router_db.user_api, router_db.pass_api, router_db.port_api)
    filas = []

    try:
        # Obtenemos directamente los usuarios PPPoE
        secrets = mk.obtener_todos_pppoe()

        for item in secrets:
            # Lógica de coincidencia de planes
            perfil_mk = item.get('profile', '') or item.get('comment', '') or ''
            perfil_limpio = perfil_mk.strip().lower()
            plan_final_id = mapa_planes.get(perfil_limpio)
            
            if not plan_final_id:
                for p_nombre, p_id in mapa_planes.items():
                    if p_nombre in perfil_limpio:
                        plan_final_id = p_id
                        break
            if not plan_final_id: plan_final_id = plan_id 

            ip = item.get('remote-address') or ''
            # En PPPoE el nombre principal es el user_pppoe, si hay comentario lo usamos como nombre del cliente
            nombre_mk = item.get('comment') or item.get('name') or f"Cliente PPPoE {ip}"

            filas.append({
                "nombre_cliente": nombre_mk, 
                "ip_asignada": ip,
                "mac_address": item.get('caller-id') or '', 
                "usuario_pppoe": item.get('name', ''),
                "password_pppoe": item.get('password', ''),
                "telefono": "",
                "direccion": "",
                "id_red": red_id,
                "id_zona": zona_id,
                "id_plantilla_facturacion": plantilla_id,
                "id_plan_sugerido": plan_final_id, 
                "perfil_mikrotik_original": perfil_mk
            })

    except Exception as e:
        print(f"⚠️ Error leyendo Mikrotik PPPoE: {e}")
    
    # 3. Generar Excel en MEMORIA (Sin guardar en disco)
    df = pd.DataFrame(filas)
    filename = f"Importacion_PPPoE_{router_db.nombre.replace(' ', '_')}.xlsx"
    
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df.to_excel(writer, index=False)
    output.seek(0)
    
    # 4. Enviar Stream al navegador
    headers = {'Content-Disposition': f'attachment; filename="{filename}"'}
    return StreamingResponse(
        output, 
        headers=headers, 
        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )

@router.post("/importar/procesar-excel")
async def procesar_importacion(
    router_id: int,
    archivo: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user = Depends(role_required(["admin"]))
):
    """Procesa el Excel subido y crea los clientes."""
    if not await db.get(RouterModel, router_id): 
        raise HTTPException(400, "Router inválido")
    
    try:
        content = await archivo.read()
        df = pd.read_excel(io.BytesIO(content))
        df.columns = [str(c).strip().lower().replace(' ', '_') for c in df.columns] 
        df = df.fillna("")

        # Caches para optimizar
        ips_db = (await db.execute(select(ClienteModel.ip_asignada))).scalars().all()
        ips_existentes = set(ips_db)
        users_db = (await db.execute(select(ClienteModel.user_pppoe))).scalars().all()
        users_existentes = set(users_db)
        
        redes_cache = {}
        count_ok = 0
        errores = []

        for idx, row in df.iterrows():
            fila = idx + 2
            try:
                red_id = int(row.get('id_red', 0))
                zona_id = int(row.get('id_zona', 0))
                plantilla_id = int(row.get('id_plantilla_facturacion', 0))
                plan_id = int(row.get('id_plan_sugerido', 0))
            except:
                errores.append(f"Fila {fila}: IDs corruptos.")
                continue

            nombre = str(row.get('nombre_cliente', '')).strip() or f"Cliente Fila {fila}"
            ip = str(row.get('ip_asignada', '')).strip()
            user_ppp = str(row.get('usuario_pppoe', '')).strip() or None

            # Validaciones básicas
            if ip and ip in ips_existentes:
                errores.append(f"Fila {fila}: IP {ip} duplicada.")
                continue

            if ip:
                if red_id not in redes_cache:
                    r_db = await db.get(RedModel, red_id)
                    if r_db: redes_cache[red_id] = ipaddress.ip_network(r_db.cidr, strict=False)
                    else:
                        errores.append(f"Fila {fila}: Red ID {red_id} no encontrada.")
                        continue
                try:
                    if ipaddress.ip_address(ip) not in redes_cache[red_id]:
                        errores.append(f"Fila {fila}: IP fuera de rango.")
                except:
                    errores.append(f"Fila {fila}: IP inválida.")
                    continue

            if user_ppp and user_ppp in users_existentes:
                errores.append(f"Fila {fila}: Usuario PPPoE duplicado.")
                continue

            nuevo = ClienteModel(
                nombre=nombre,
                telefono=str(row.get('telefono', '')),
                direccion=str(row.get('direccion', '')),
                user_pppoe=user_ppp,
                pass_pppoe=str(row.get('password_pppoe', '12345')),
                ip_asignada=ip,
                mac_address=str(row.get('mac_address', '')),
                router_id=router_id,
                zona_id=zona_id if zona_id > 0 else None,
                plantilla_id=plantilla_id if plantilla_id > 0 else None,
                plan_id=plan_id if plan_id > 0 else None,
                estado="activo"
            )
            db.add(nuevo)
            if ip: ips_existentes.add(ip)
            if user_ppp: users_existentes.add(user_ppp)
            count_ok += 1

        await db.commit()
        return {"importados": count_ok, "errores": errores}

    except Exception as e:
        await db.rollback()
        raise HTTPException(500, f"Error procesando archivo: {str(e)}")


# ==========================================
# 4. DIAGNÓSTICO Y MONITOREO
# ==========================================

@router.get("/diagnostico/tecnico/{cliente_id}")
async def obtener_diagnostico_completo(cliente_id: int, db: AsyncSession = Depends(get_db)):
    service = NetworkService(db)
    try:
        return await service.obtener_estado_tecnico(cliente_id)
    except Exception as e:
        raise HTTPException(500, str(e))

@router.get("/diagnostico/ping/{cliente_id}")
async def realizar_ping_cliente(cliente_id: int, db: AsyncSession = Depends(get_db)):
    service = NetworkService(db)
    try:
        return await service.ping_cliente(cliente_id)
    except Exception as e:
        raise HTTPException(500, str(e))

@router.get("/diagnostico/conexion/{cliente_id}")
async def verificar_conexion_cliente(cliente_id: int, db: AsyncSession = Depends(get_db)):
    service = NetworkService(db)
    try:
        return await service.verificar_conexion(cliente_id)
    except Exception as e:
        return {"online": False, "mensaje": "Error de consulta", "error": str(e)}

@router.get("/diagnostico/trafico/{cliente_id}")
async def verificar_trafico_cliente(cliente_id: int, db: AsyncSession = Depends(get_db)):
    service = NetworkService(db)
    try:
        return await service.verificar_trafico(cliente_id)
    except Exception as e:
        # 🚀 Esto imprimirá el error real en la consola en lugar de ocultarlo
        print(f"❌ Error al consultar tráfico del cliente {cliente_id}: {e}") 
        return {"velocidad_subida": 0, "velocidad_bajada": 0}
    


