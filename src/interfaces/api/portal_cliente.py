"""Portal del cliente (/portal): su cuenta, su consumo y la red de su casa.

La sesión va en la cookie fdeznet_portal, aparte de la del personal. Hasta
que el cliente pone su propia contraseña solo puede cambiarla.
"""

import asyncio
import logging
from datetime import date

import httpx
import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.services.portal_cliente_service import SESION_HORAS, PortalClienteService
from src.infrastructure.auth import LoginRateLimiter
from src.infrastructure.database import get_db
from src.infrastructure.models import ServicioModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/portal", tags=["Portal del cliente"])

COOKIE = "fdeznet_portal"
# 5 intentos por contrato y 20 por IP cada 15 minutos: el contrato son 4
# caracteres y se puede adivinar si no se frena.
intentos_contrato = LoginRateLimiter(max_attempts=5, window_seconds=900)
intentos_ip = LoginRateLimiter(max_attempts=20, window_seconds=900)
# Acciones sobre su equipo: que un clic repetido no reinicie el módem cada minuto.
reinicios = LoginRateLimiter(max_attempts=1, window_seconds=600)
cambios_red = LoginRateLimiter(max_attempts=20, window_seconds=600)
reportes = LoginRateLimiter(max_attempts=3, window_seconds=3600)
codigos_telefono = LoginRateLimiter(max_attempts=3, window_seconds=3600)
# La gráfica en vivo pregunta cada pocos segundos; esto evita que alguien sature el MikroTik.
lecturas_trafico = LoginRateLimiter(max_attempts=40, window_seconds=60)

CATEGORIAS_PORTAL = {"sin_internet", "lentitud", "router_wifi", "otro"}
compras_fichas = LoginRateLimiter(max_attempts=10, window_seconds=600)


def rutas_categorias():
    return [("sin_internet", "Sin internet"), ("lentitud", "Lentitud"), ("router_wifi", "WiFi o módem"), ("otro", "Otro")]


class Entrada(BaseModel):
    contrato: str
    clave: str


class CambioClave(BaseModel):
    actual: str
    nueva: str


class Bloqueo(BaseModel):
    mac: str
    bloquear: bool = True


class ClaveWifi(BaseModel):
    ssid: str
    clave: str


class Falla(BaseModel):
    categoria: str
    descripcion: str


class Promesa(BaseModel):
    fecha: date


class Telefono(BaseModel):
    telefono: str


class Codigo(BaseModel):
    codigo: str


def _ip(request: Request) -> str:
    return request.client.host if request.client else "desconocida"


def _guardar_cookie(request: Request, response: Response, token: str) -> None:
    response.set_cookie(
        key=COOKIE, value=token, max_age=SESION_HORAS * 3600, httponly=True,
        secure=request.headers.get("x-forwarded-proto", request.url.scheme) == "https",
        samesite="strict", path="/",
    )


async def sesion_portal(request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    token = request.cookies.get(COOKIE, "")
    try:
        return await PortalClienteService(db).sesion(token)
    except (jwt.PyJWTError, ValueError, TypeError, PermissionError) as error:
        detalle = str(error) if isinstance(error, PermissionError) else "Entra con tu contrato y contraseña."
        raise HTTPException(status_code=401, detail=detalle) from error


async def cliente_portal(sesion: dict = Depends(sesion_portal)):
    """Todo lo demás pide que ya haya puesto su propia contraseña."""
    if sesion["debe_cambiar"]:
        raise HTTPException(status_code=403, detail="Primero cambia tu contraseña.")
    return sesion["cliente"]


def _error(error: Exception) -> HTTPException:
    if isinstance(error, (ValueError, LookupError)):
        return HTTPException(status_code=409, detail=str(error))
    return HTTPException(status_code=502, detail="Tu módem no respondió; intenta en un momento.")


@router.post("/entrar")
async def entrar(datos: Entrada, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    llave_contrato, llave_ip = datos.contrato.strip().upper(), _ip(request)
    intentos_ip.ensure_allowed(llave_ip)
    intentos_contrato.ensure_allowed(llave_contrato)
    try:
        resultado = await PortalClienteService(db).entrar(datos.contrato, datos.clave)
    except ValueError as error:
        intentos_ip.register_failure(llave_ip)
        intentos_contrato.register_failure(llave_contrato)
        raise HTTPException(status_code=401, detail=str(error)) from error
    intentos_contrato.reset(llave_contrato)
    _guardar_cookie(request, response, resultado["token"])
    return {"debe_cambiar": resultado["debe_cambiar"]}


@router.post("/salir", status_code=204)
async def salir(response: Response):
    response.delete_cookie(COOKIE, path="/")


@router.get("/sesion")
async def sesion(datos: dict = Depends(sesion_portal)):
    return {"nombre": datos["cliente"].nombre, "contrato": datos["cliente"].cedula, "debe_cambiar": datos["debe_cambiar"]}


@router.post("/clave")
async def cambiar_clave(datos: CambioClave, request: Request, response: Response,
                        sesion: dict = Depends(sesion_portal), db: AsyncSession = Depends(get_db)):
    llave = f"clave:{sesion['cliente'].id}"
    intentos_contrato.ensure_allowed(llave)
    try:
        resultado = await PortalClienteService(db).cambiar_clave(sesion["cliente"], datos.actual, datos.nueva)
    except ValueError as error:
        intentos_contrato.register_failure(llave)
        raise HTTPException(status_code=409, detail=str(error)) from error
    _guardar_cookie(request, response, resultado["token"])
    return {"debe_cambiar": False}


@router.get("/resumen")
async def resumen(cliente=Depends(cliente_portal), db: AsyncSession = Depends(get_db)):
    return await PortalClienteService(db).resumen(cliente.id)


@router.get("/consumo")
async def consumo(mes: str = Query(..., pattern=r"^\d{4}-\d{2}$"), cliente=Depends(cliente_portal),
                  db: AsyncSession = Depends(get_db)):
    from src.application.services.consumo_service import ConsumoService

    anio, numero = (int(parte) for parte in mes.split("-"))
    if not 1 <= numero <= 12 or date(anio, numero, 1) > date.today():
        raise HTTPException(status_code=422, detail="Mes inválido")
    return await ConsumoService(db).del_mes(cliente.id, anio, numero)


@router.get("/red")
async def red(cliente=Depends(cliente_portal), db: AsyncSession = Depends(get_db)):
    from src.application.services.red_cliente_service import RedClienteService

    try:
        return await RedClienteService(db).estado(cliente.id, solo_principal=True)
    except (LookupError, ValueError, httpx.HTTPError) as error:
        raise _error(error) from error


@router.post("/red/bloqueo")
async def bloqueo(datos: Bloqueo, cliente=Depends(cliente_portal), db: AsyncSession = Depends(get_db)):
    from src.application.services.red_cliente_service import RedClienteService

    cambios_red.ensure_allowed(str(cliente.id))
    cambios_red.register_failure(str(cliente.id))
    try:
        return await RedClienteService(db).bloquear(cliente.id, datos.mac, datos.bloquear, None, solo_principal=True)
    except (LookupError, ValueError, httpx.HTTPError) as error:
        raise _error(error) from error


@router.post("/red/clave-wifi")
async def clave_wifi(datos: ClaveWifi, cliente=Depends(cliente_portal), db: AsyncSession = Depends(get_db)):
    from src.application.services.red_cliente_service import RedClienteService

    cambios_red.ensure_allowed(str(cliente.id))
    cambios_red.register_failure(str(cliente.id))
    try:
        return await RedClienteService(db).cambiar_clave_wifi(cliente.id, datos.ssid, datos.clave, None, solo_principal=True)
    except (LookupError, ValueError, httpx.HTTPError) as error:
        raise _error(error) from error


@router.post("/reiniciar")
async def reiniciar(cliente=Depends(cliente_portal), db: AsyncSession = Depends(get_db)):
    from src.application.services.red_cliente_service import RedClienteService

    reinicios.ensure_allowed(str(cliente.id))
    try:
        resultado = await RedClienteService(db).reiniciar(cliente.id, None)
    except (LookupError, ValueError, httpx.HTTPError) as error:
        raise _error(error) from error
    reinicios.register_failure(str(cliente.id))
    return resultado


@router.post("/falla", status_code=201)
async def reportar_falla(datos: Falla, cliente=Depends(cliente_portal), db: AsyncSession = Depends(get_db)):
    from src.application.services.support_service import SupportService
    from src.application.services.usuario_sistema_service import usuario_portal_cliente

    if datos.categoria not in CATEGORIAS_PORTAL:
        raise HTTPException(status_code=422, detail="Elige qué te pasa.")
    descripcion = (datos.descripcion or "").strip()
    if len(descripcion) < 5:
        raise HTTPException(status_code=422, detail="Cuéntanos brevemente qué pasa.")
    reportes.ensure_allowed(str(cliente.id))
    try:
        orden = await SupportService(db).crear_incidencia(
            cliente_id=cliente.id, categoria=datos.categoria,
            descripcion=f"[Portal del cliente] {descripcion[:1000]}",
            usuario=await usuario_portal_cliente(db), canal_reporte="portal",
        )
    except RuntimeError as error:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Ya tienes un reporte abierto por eso; un técnico lo está atendiendo.") from error
    except ValueError as error:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(error)) from error
    reportes.register_failure(str(cliente.id))
    orden_id = orden.get("id") if isinstance(orden, dict) else getattr(orden, "id", None)
    nombres = dict(rutas_categorias())
    await PortalClienteService(db).avisar_personal(
        f"🛠️ *Nuevo reporte desde el portal* #{orden_id}\n👤 {cliente.nombre} (contrato {cliente.cedula})\n"
        f"🏷️ {nombres.get(datos.categoria, datos.categoria)}\n📝 {descripcion[:500]}"
    )
    return {"id": orden_id}


@router.get("/facturas")
async def facturas(cliente=Depends(cliente_portal), db: AsyncSession = Depends(get_db)):
    return await PortalClienteService(db).facturas(cliente.id)


@router.get("/facturas/{factura_id}/pdf")
async def factura_pdf(factura_id: int, cliente=Depends(cliente_portal), db: AsyncSession = Depends(get_db)):
    from src.application.services.factura_pdf_service import nombre_pdf, pdf_de_factura

    try:
        factura = await PortalClienteService(db).factura_con_cliente(cliente.id, factura_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return Response(content=await pdf_de_factura(db, factura), media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="{nombre_pdf(factura)}"', "Cache-Control": "no-store",
    })


@router.get("/promesa")
async def opciones_promesa(cliente=Depends(cliente_portal), db: AsyncSession = Depends(get_db)):
    return await PortalClienteService(db).opciones_promesa(cliente)


@router.post("/promesa")
async def registrar_promesa(datos: Promesa, cliente=Depends(cliente_portal), db: AsyncSession = Depends(get_db)):
    try:
        return await PortalClienteService(db).registrar_promesa(cliente, datos.fecha)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/reportes")
async def mis_reportes(cliente=Depends(cliente_portal), db: AsyncSession = Depends(get_db)):
    return await PortalClienteService(db).reportes(cliente.id)


@router.get("/trafico")
async def trafico(cliente=Depends(cliente_portal), db: AsyncSession = Depends(get_db)):
    """Lo que está usando en este momento (bits por segundo), leído de su sesión PPPoE."""
    from src.application.services.network_service import NetworkService

    lecturas_trafico.ensure_allowed(str(cliente.id))
    lecturas_trafico.register_failure(str(cliente.id))
    servicio_id = (await db.execute(
        select(ServicioModel.id).where(ServicioModel.cliente_id == cliente.id, ServicioModel.estado != "cancelado",
                                       ServicioModel.user_pppoe.isnot(None)).order_by(ServicioModel.id).limit(1)
    )).scalar_one_or_none()
    try:
        servicio_red = NetworkService(db)
        mk, servicio = await (servicio_red._get_mk_session_servicio(servicio_id) if servicio_id else servicio_red._get_mk_session(cliente.id))
        usuario_pppoe = servicio.user_pppoe
        # La API del MikroTik bloquea: en un hilo para que muchos clientes mirando no frenen el servidor.
        consumo = (await asyncio.to_thread(mk.obtener_consumo_interfaz_pppoe, usuario_pppoe) if usuario_pppoe else None) or {}
    except Exception as error:  # el MikroTik no contestó: se muestra en cero, como en la ficha
        logger.warning("Tráfico en vivo del cliente %s: %s", cliente.id, error)
        return {"bajada_bps": 0, "subida_bps": 0, "disponible": False}
    return {"bajada_bps": consumo.get("down_bps", 0), "subida_bps": consumo.get("up_bps", 0), "disponible": bool(usuario_pppoe)}


@router.post("/telefono/codigo")
async def pedir_codigo(datos: Telefono, cliente=Depends(cliente_portal), db: AsyncSession = Depends(get_db)):
    codigos_telefono.ensure_allowed(str(cliente.id))
    try:
        resultado = await PortalClienteService(db).pedir_codigo_telefono(cliente, datos.telefono)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    codigos_telefono.register_failure(str(cliente.id))
    return resultado


@router.post("/telefono/confirmar")
async def confirmar_codigo(datos: Codigo, cliente=Depends(cliente_portal), db: AsyncSession = Depends(get_db)):
    try:
        return await PortalClienteService(db).confirmar_codigo_telefono(cliente, datos.codigo)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


class Suscripcion(BaseModel):
    endpoint: str
    keys: dict


class Cancelacion(BaseModel):
    endpoint: str


@router.get("/notificaciones")
async def notificaciones(cliente=Depends(cliente_portal), db: AsyncSession = Depends(get_db)):
    """La llave pública para que el navegador se suscriba (se crea la primera vez)."""
    from src.application.services.push_service import PushService, llaves

    config = await llaves(db)
    return {"disponible": True, "clave_publica": config["publica"], "dispositivos": await PushService(db).cuantas(cliente.id)}


@router.post("/notificaciones")
async def activar_notificaciones(datos: Suscripcion, cliente=Depends(cliente_portal), db: AsyncSession = Depends(get_db)):
    from src.application.services.push_service import PushService

    try:
        await PushService(db).suscribir(cliente.id, datos.model_dump())
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"ok": True}


@router.post("/notificaciones/quitar")
async def quitar_notificaciones(datos: Cancelacion, cliente=Depends(cliente_portal), db: AsyncSession = Depends(get_db)):
    from src.application.services.push_service import PushService

    await PushService(db).cancelar(cliente.id, datos.endpoint)
    return {"ok": True}


@router.post("/notificaciones/prueba")
async def probar_notificaciones(cliente=Depends(cliente_portal), db: AsyncSession = Depends(get_db)):
    from src.application.services.push_service import PushService

    reportes.ensure_allowed(f"prueba:{cliente.id}")
    reportes.register_failure(f"prueba:{cliente.id}")
    enviadas = await PushService(db).enviar(cliente.id, "Notificaciones activadas", "Aquí te avisaremos de tus pagos y de tu servicio.")
    if not enviadas:
        raise HTTPException(status_code=409, detail="No se pudo enviar a este celular.")
    return {"enviadas": enviadas}


class CompraFicha(BaseModel):
    perfil_id: str


@router.get("/fichas")
async def fichas(cliente=Depends(cliente_portal), db: AsyncSession = Depends(get_db)):
    """Fichas de Zona FdezNet que puede comprar (se cobran en su factura) y las que ya compró."""
    servicio = PortalClienteService(db)
    try:
        disponibles = await servicio.fichas_disponibles()
    except (ValueError, httpx.HTTPError) as error:
        logger.warning("Sistema de fichas: %s", error)
        disponibles = {"disponible": False, "perfiles": []}
    return {**disponibles, "compradas": await servicio.mis_fichas(cliente.id)}


@router.post("/fichas")
async def comprar_ficha(datos: CompraFicha, cliente=Depends(cliente_portal), db: AsyncSession = Depends(get_db)):
    # Sin límite de compra (así lo pidió el ISP); esto solo evita compras por doble toque.
    compras_fichas.ensure_allowed(str(cliente.id))
    compras_fichas.register_failure(str(cliente.id))
    try:
        return await PortalClienteService(db).comprar_ficha(cliente, datos.perfil_id)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except httpx.HTTPError as error:
        raise HTTPException(status_code=502, detail="El sistema de fichas no respondió; intenta en un momento.") from error
