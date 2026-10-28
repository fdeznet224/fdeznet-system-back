"""Panel central de la nube: solo para el dueño.

Da de alta a cada ISP con su subdominio, lleva su mensualidad, lo suspende o
reactiva y le aplica actualizaciones. Corre aparte de las instancias.
"""
import hashlib
import hmac
import logging
import os
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import jwt
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from passlib.hash import pbkdf2_sha256
from sqlalchemy import select

from nube.panel import docker_ops
from nube.panel.cuentas import (
    PRIMER_PUERTO_WEB,
    PRIMER_PUERTO_WG,
    SLUG,
    estado_cuenta,
    estado_respaldo,
    nuevo_vencimiento,
    segundos_hasta,
    siguiente_puerto,
)
from nube.panel.db import Bitacora, Isp, Pago, Respaldo, Sesion, crear_tablas

USUARIO = os.getenv("PANEL_USUARIO", "dueno")
CLAVE_HASH = os.getenv("PANEL_CLAVE_HASH", "")
SECRETO = os.getenv("PANEL_SECRETO", "")
DOMINIO_BASE = os.getenv("PANEL_DOMINIO_BASE", "fdezpay.com")
DIAS_GRACIA = int(os.getenv("PANEL_DIAS_GRACIA", "5"))
COOKIE = "panel_sesion"
HORAS_SESION = 12
COOKIE_SEGURA = os.getenv("PANEL_COOKIE_SEGURA", "1") == "1"
HORA_RESPALDO = os.getenv("PANEL_HORA_RESPALDO", "03:00")  # vacío: sin respaldo diario

if not CLAVE_HASH or len(SECRETO) < 32:
    raise RuntimeError("Configura PANEL_CLAVE_HASH y PANEL_SECRETO (32+ caracteres)")

AQUI = Path(__file__).resolve().parent
log = logging.getLogger("panel")


@asynccontextmanager
async def ciclo_de_vida(_app: FastAPI):
    if HORA_RESPALDO:
        threading.Thread(target=_respaldos_diarios, daemon=True, name="respaldos").start()
    yield


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=ciclo_de_vida)
app.mount("/static", StaticFiles(directory=AQUI / "static"), name="static")
plantillas = Jinja2Templates(directory=AQUI / "templates")
plantillas.env.filters["dinero"] = lambda valor: f"${Decimal(valor or 0):,.2f}"
plantillas.env.filters["fecha"] = lambda valor: valor.strftime("%d/%m/%Y") if valor else "—"
plantillas.env.filters["tamano"] = lambda b: f"{b / 1048576:.1f} MB" if b >= 1048576 else f"{b / 1024:.0f} KB"

crear_tablas()


# --- Sesión ---------------------------------------------------------------

_intentos: dict[str, deque] = defaultdict(deque)
MAX_INTENTOS, VENTANA = 5, 15 * 60


def _bloqueado(ip: str) -> bool:
    cola = _intentos[ip]
    while cola and cola[0] < time.monotonic() - VENTANA:
        cola.popleft()
    return len(cola) >= MAX_INTENTOS


def _csrf(token: str) -> str:
    return hmac.new(SECRETO.encode(), token.encode(), hashlib.sha256).hexdigest()[:32]


async def sesion(request: Request) -> str:
    token = request.cookies.get(COOKIE, "")
    try:
        jwt.decode(token, SECRETO, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(status_code=303, headers={"Location": "/entrar"})
    if request.method == "POST":
        # Los formularios llevan una firma ligada a la sesión (contra CSRF).
        recibido = (await request.form()).get("csrf")
        if not hmac.compare_digest(str(recibido or ""), _csrf(token)):
            raise HTTPException(status_code=403, detail="Formulario vencido; recarga la página")
    return _csrf(token)


@app.middleware("http")
async def cabeceras_seguras(request: Request, siguiente):
    respuesta = await siguiente(request)
    respuesta.headers["X-Frame-Options"] = "DENY"
    respuesta.headers["X-Content-Type-Options"] = "nosniff"
    respuesta.headers["Referrer-Policy"] = "same-origin"
    return respuesta


def _render(request: Request, plantilla: str, csrf: str | None = None, **datos) -> HTMLResponse:
    return plantillas.TemplateResponse(
        request, plantilla, {"csrf": csrf, "dominio_base": DOMINIO_BASE, **datos}
    )


def _ir(ruta: str, mensaje: str = "") -> RedirectResponse:
    destino = f"{ruta}?msg={mensaje}" if mensaje else ruta
    return RedirectResponse(destino, status_code=303)


@app.get("/entrar", response_class=HTMLResponse)
def entrar_form(request: Request):
    return _render(request, "entrar.html")


@app.post("/entrar")
def entrar(request: Request, usuario: str = Form(...), clave: str = Form(...)):
    ip = request.client.host if request.client else "?"
    if _bloqueado(ip):
        return _render(request, "entrar.html", error="Demasiados intentos. Espera 15 minutos.")
    if not (hmac.compare_digest(usuario, USUARIO) and pbkdf2_sha256.verify(clave, CLAVE_HASH)):
        _intentos[ip].append(time.monotonic())
        return _render(request, "entrar.html", error="Usuario o contraseña incorrectos")
    _intentos.pop(ip, None)
    token = jwt.encode(
        {"sub": USUARIO, "exp": datetime.now(timezone.utc) + timedelta(hours=HORAS_SESION)},
        SECRETO,
        algorithm="HS256",
    )
    respuesta = _ir("/")
    respuesta.set_cookie(
        COOKIE, token, max_age=HORAS_SESION * 3600, httponly=True,
        secure=COOKIE_SEGURA, samesite="strict",
    )
    return respuesta


@app.post("/salir")
def salir(_csrf_ok: str = Depends(sesion)):
    respuesta = _ir("/entrar")
    respuesta.delete_cookie(COOKIE)
    return respuesta


# --- Lista e inicio -------------------------------------------------------

def _bitacora(db, isp_id: int | None, accion: str, detalle: str = "") -> None:
    db.add(Bitacora(isp_id=isp_id, accion=accion, detalle=detalle))


def _estado(isp: Isp):
    return estado_cuenta(isp.pagado_hasta, isp.suspendido, date.today(), dias_gracia=DIAS_GRACIA)


def _contenedores() -> tuple[dict, dict, str | None]:
    try:
        return docker_ops.estado_contenedores(), docker_ops.uso_memoria(), None
    except (docker_ops.ErrorDocker, OSError) as error:
        return {}, {}, f"No se pudo consultar Docker: {error}"


@app.get("/", response_class=HTMLResponse)
def inicio(request: Request, msg: str = "", csrf: str = Depends(sesion)):
    with Sesion() as db:
        isps = db.scalars(select(Isp).order_by(Isp.nombre)).all()
        respaldos = {isp.id: _estado_respaldo(db, isp) for isp in isps}
    contenedores, memoria, error = _contenedores()
    filas = []
    for isp in isps:
        servicios = contenedores.get(isp.slug, {})
        filas.append({
            "isp": isp,
            "estado": _estado(isp),
            "en_linea": servicios.get("api", "").startswith("Up"),
            "ram": sum(mb for nombre, mb in memoria.items() if nombre.startswith(f"{isp.slug}-")),
            "respaldo": respaldos[isp.id],
        })
    resumen = {
        "total": len(isps),
        "mensual": sum((isp.precio_mensual for isp in isps if not isp.suspendido), Decimal(0)),
        "atencion": sum(1 for f in filas if f["estado"].clave in {"por_vencer", "gracia", "vencido", "sin_pago"}),
        "ram": sum(memoria.values()),
        "respaldos": sum(1 for r in respaldos.values() if r.tono in {"rojo", "ambar"}),
    }
    return _render(request, "inicio.html", csrf, filas=filas, resumen=resumen, msg=msg, error=error)


# --- Alta -----------------------------------------------------------------

def _entero(valor: str) -> int | None:
    valor = (valor or "").strip()
    return int(valor) if valor.isdigit() else None


def _dinero(valor: str) -> Decimal:
    try:
        return Decimal((valor or "0").replace(",", "").replace("$", "").strip() or "0")
    except InvalidOperation:
        raise HTTPException(status_code=400, detail="Monto inválido")


@app.get("/isp/nuevo", response_class=HTMLResponse)
def nuevo_form(request: Request, csrf: str = Depends(sesion)):
    try:
        versiones = docker_ops.versiones_disponibles()
    except (docker_ops.ErrorDocker, OSError):
        versiones = []
    return _render(request, "nuevo.html", csrf, versiones=versiones or ["local"])


@app.post("/isp/nuevo", response_class=HTMLResponse)
def nuevo(
    request: Request,
    csrf: str = Depends(sesion),
    nombre: str = Form(...),
    slug: str = Form(...),
    contacto: str = Form(""),
    telefono: str = Form(""),
    plan: str = Form(""),
    precio_mensual: str = Form("0"),
    dia_pago: int = Form(1),
    limite_clientes: str = Form(""),
    limite_routers: str = Form(""),
    whatsapp: bool = Form(False),
    version: str = Form("local"),
):
    slug = slug.strip().lower()
    if not SLUG.fullmatch(slug):
        raise HTTPException(status_code=400, detail="Subdominio inválido: minúsculas, números y guiones")
    with Sesion() as db:
        if db.scalar(select(Isp).where(Isp.slug == slug)):
            raise HTTPException(status_code=400, detail="Ese subdominio ya existe")
        usados = db.execute(select(Isp.web_port, Isp.wg_port)).all()
        web_disco, wg_disco = docker_ops.puertos_en_uso()
        isp = Isp(
            nombre=nombre.strip(), slug=slug, dominio=f"{slug}.{DOMINIO_BASE}",
            contacto=contacto.strip() or None, telefono=telefono.strip() or None,
            plan=plan.strip() or None, precio_mensual=_dinero(precio_mensual),
            dia_pago=min(max(dia_pago, 1), 31),
            limite_clientes=_entero(limite_clientes), limite_routers=_entero(limite_routers),
            whatsapp=whatsapp, version=version,
            web_port=siguiente_puerto([u.web_port for u in usados] + web_disco, PRIMER_PUERTO_WEB),
            wg_port=siguiente_puerto([u.wg_port for u in usados] + wg_disco, PRIMER_PUERTO_WG),
        )
        try:
            clave = docker_ops.crear(
                isp.slug, isp.web_port, isp.wg_port, isp.dominio,
                limite_clientes=isp.limite_clientes, limite_routers=isp.limite_routers,
                whatsapp=isp.whatsapp, version=isp.version,
            )
        except (docker_ops.ErrorDocker, OSError) as error:
            return _render(request, "nuevo.html", csrf, versiones=[version], error=str(error), previo=isp)
        db.add(isp)
        db.flush()
        _bitacora(db, isp.id, "alta", f"{isp.dominio} · web {isp.web_port} · WireGuard {isp.wg_port}")
        db.commit()
    # La contraseña se muestra una sola vez; no se guarda en el panel.
    return _render(request, "creado.html", csrf, isp=isp, clave=clave)


# --- Detalle y acciones ---------------------------------------------------

def _isp(db, isp_id: int) -> Isp:
    isp = db.get(Isp, isp_id)
    if not isp:
        raise HTTPException(status_code=404, detail="ISP no encontrado")
    return isp


@app.get("/isp/{isp_id}", response_class=HTMLResponse)
def detalle(request: Request, isp_id: int, msg: str = "", csrf: str = Depends(sesion)):
    with Sesion() as db:
        isp = _isp(db, isp_id)
        pagos = list(isp.pagos)
        bitacora = db.scalars(
            select(Bitacora).where(Bitacora.isp_id == isp_id).order_by(Bitacora.fecha.desc()).limit(20)
        ).all()
        respaldo = _estado_respaldo(db, isp)
    contenedores, memoria, error = _contenedores()
    try:
        versiones = docker_ops.versiones_disponibles()
    except (docker_ops.ErrorDocker, OSError):
        versiones = []
    if isp.version not in versiones:
        versiones.append(isp.version)
    servicios = [
        {"nombre": nombre, "estado": contenedores.get(isp.slug, {}).get(clave, "Sin crear"),
         "ram": memoria.get(f"{isp.slug}-{clave}-1")}
        for clave, nombre in (("api", "API"), ("bot", "WhatsApp"), ("web", "Web"))
        if clave != "bot" or isp.whatsapp
    ]
    return _render(
        request, "detalle.html", csrf, isp=isp, estado=_estado(isp), pagos=pagos,
        bitacora=bitacora, servicios=servicios, versiones=versiones, msg=msg, error=error,
        hoy=date.today(), respaldo=respaldo, respaldos=docker_ops.listar_respaldos(isp.slug),
        hora_respaldo=HORA_RESPALDO,
    )


@app.post("/isp/{isp_id}/pago")
def registrar_pago(
    isp_id: int,
    _csrf_ok: str = Depends(sesion),
    monto: str = Form(...),
    meses: int = Form(1),
    fecha: date = Form(...),
    nota: str = Form(""),
):
    if not 1 <= meses <= 24:
        raise HTTPException(status_code=400, detail="Meses entre 1 y 24")
    with Sesion() as db:
        isp = _isp(db, isp_id)
        cubre = nuevo_vencimiento(isp.pagado_hasta, meses, isp.dia_pago, fecha)
        db.add(Pago(isp_id=isp.id, fecha=fecha, monto=_dinero(monto), meses=meses,
                    cubre_hasta=cubre, nota=nota.strip() or None))
        isp.pagado_hasta = cubre
        _bitacora(db, isp.id, "pago", f"{meses} mes(es), {_dinero(monto)} · cubre hasta {cubre:%d/%m/%Y}")
        db.commit()
    return _ir(f"/isp/{isp_id}", f"Pago registrado: cubre hasta {cubre:%d/%m/%Y}")


@app.post("/isp/{isp_id}/pago/{pago_id}/borrar")
def borrar_pago(isp_id: int, pago_id: int, _csrf_ok: str = Depends(sesion)):
    """Quita un pago capturado por error y regresa la fecha a como estaba."""
    with Sesion() as db:
        isp = _isp(db, isp_id)
        pago = db.get(Pago, pago_id)
        if not pago or pago.isp_id != isp.id or isp.pagos[0].id != pago.id:
            raise HTTPException(status_code=400, detail="Solo se puede borrar el pago más reciente")
        anterior = isp.pagos[1].cubre_hasta if len(isp.pagos) > 1 else None
        isp.pagado_hasta = anterior
        db.delete(pago)
        _bitacora(db, isp.id, "pago borrado", f"{pago.monto} del {pago.fecha:%d/%m/%Y}")
        db.commit()
    return _ir(f"/isp/{isp_id}", "Pago borrado")


@app.post("/isp/{isp_id}/datos")
def guardar_datos(
    isp_id: int,
    _csrf_ok: str = Depends(sesion),
    nombre: str = Form(...),
    contacto: str = Form(""),
    telefono: str = Form(""),
    plan: str = Form(""),
    precio_mensual: str = Form("0"),
    dia_pago: int = Form(1),
    pagado_hasta: str = Form(""),
    notas: str = Form(""),
):
    with Sesion() as db:
        isp = _isp(db, isp_id)
        isp.nombre = nombre.strip()
        isp.contacto = contacto.strip() or None
        isp.telefono = telefono.strip() or None
        isp.plan = plan.strip() or None
        isp.precio_mensual = _dinero(precio_mensual)
        isp.dia_pago = min(max(dia_pago, 1), 31)
        nueva = date.fromisoformat(pagado_hasta) if pagado_hasta else None
        if nueva != isp.pagado_hasta:
            _bitacora(db, isp.id, "fecha corregida", f"{isp.pagado_hasta} → {nueva}")
            isp.pagado_hasta = nueva
        isp.notas = notas.strip() or None
        db.commit()
    return _ir(f"/isp/{isp_id}", "Datos guardados")


@app.post("/isp/{isp_id}/ajustes")
def guardar_ajustes(
    isp_id: int,
    _csrf_ok: str = Depends(sesion),
    version: str = Form(...),
    whatsapp: bool = Form(False),
    limite_clientes: str = Form(""),
    limite_routers: str = Form(""),
):
    with Sesion() as db:
        isp = _isp(db, isp_id)
        antes = f"v{isp.version}, bot {'sí' if isp.whatsapp else 'no'}, límites {isp.limite_clientes}/{isp.limite_routers}"
        try:
            docker_ops.aplicar_ajustes(
                isp.slug, version=version, whatsapp=whatsapp,
                limite_clientes=_entero(limite_clientes), limite_routers=_entero(limite_routers),
                encendido=not isp.suspendido,
            )
        except (docker_ops.ErrorDocker, OSError) as error:
            return _ir(f"/isp/{isp_id}", f"Error: {error}")
        isp.version, isp.whatsapp = version, whatsapp
        isp.limite_clientes, isp.limite_routers = _entero(limite_clientes), _entero(limite_routers)
        despues = f"v{isp.version}, bot {'sí' if isp.whatsapp else 'no'}, límites {isp.limite_clientes}/{isp.limite_routers}"
        _bitacora(db, isp.id, "ajustes", f"{antes} → {despues}")
        db.commit()
    return _ir(f"/isp/{isp_id}", "Ajustes aplicados")


@app.post("/isp/{isp_id}/suspender")
def suspender(isp_id: int, _csrf_ok: str = Depends(sesion)):
    with Sesion() as db:
        isp = _isp(db, isp_id)
        try:
            docker_ops.detener(isp.slug)
        except (docker_ops.ErrorDocker, OSError) as error:
            return _ir(f"/isp/{isp_id}", f"Error: {error}")
        isp.suspendido = True
        _bitacora(db, isp.id, "suspendido")
        db.commit()
    return _ir(f"/isp/{isp_id}", "ISP suspendido; sus datos se conservan")


@app.post("/isp/{isp_id}/reactivar")
def reactivar(isp_id: int, _csrf_ok: str = Depends(sesion)):
    with Sesion() as db:
        isp = _isp(db, isp_id)
        try:
            docker_ops.encender(isp.slug)
        except (docker_ops.ErrorDocker, OSError) as error:
            return _ir(f"/isp/{isp_id}", f"Error: {error}")
        isp.suspendido = False
        _bitacora(db, isp.id, "reactivado")
        db.commit()
    return _ir(f"/isp/{isp_id}", "ISP reactivado")


# --- Respaldos ------------------------------------------------------------

_ocupados: set[str] = set()
_candado = threading.Lock()


def _estado_respaldo(db, isp: Isp):
    ultimo = db.scalar(
        select(Respaldo).where(Respaldo.isp_id == isp.id).order_by(Respaldo.id.desc()).limit(1)
    )
    if ultimo and ultimo.error:
        return estado_respaldo(ultimo.fecha, ultimo.error, None, isp.creado, datetime.now())
    return estado_respaldo(
        ultimo.fecha if ultimo else None, None, ultimo.remoto if ultimo else None,
        isp.creado, datetime.now(),
    )


def _respaldar(isp_id: int, slug: str) -> str | None:
    """Respalda un ISP y lo anota; devuelve el error o None. Uno a la vez por ISP."""
    with _candado:
        if slug in _ocupados:
            return "Ya hay un respaldo o restauración en curso"
        _ocupados.add(slug)
    try:
        datos = docker_ops.respaldar(slug)
        error = None
    except Exception as fallo:  # noqa: BLE001 - cualquier falla se anota en el panel
        datos, error = {}, str(fallo)[-500:] or fallo.__class__.__name__
    finally:
        with _candado:
            _ocupados.discard(slug)
    with Sesion() as db:
        db.add(Respaldo(
            isp_id=isp_id, archivo=datos.get("ARCHIVO"), remoto=datos.get("REMOTO"), error=error,
            tamano=int(datos["TAMANO"]) if datos.get("TAMANO", "").isdigit() else None,
        ))
        if error:
            _bitacora(db, isp_id, "respaldo falló", error)
        db.commit()
    return error


def _respaldos_diarios() -> None:
    """Hilo del panel: a la HORA_RESPALDO respalda a todos los ISP, uno tras otro."""
    while True:
        time.sleep(segundos_hasta(HORA_RESPALDO, datetime.now()))
        try:
            with Sesion() as db:
                isps = [(isp.id, isp.slug) for isp in db.scalars(select(Isp).order_by(Isp.id))]
            for isp_id, slug in isps:
                error = _respaldar(isp_id, slug)
                if error:
                    log.error("Respaldo de %s falló: %s", slug, error)
        except Exception:  # noqa: BLE001 - el hilo no debe morir; mañana lo intenta otra vez
            log.exception("Falló la ronda de respaldos")
        time.sleep(61)  # que no repita dentro del mismo minuto


@app.post("/isp/{isp_id}/respaldar")
def respaldar_ahora(isp_id: int, _csrf_ok: str = Depends(sesion)):
    with Sesion() as db:
        isp = _isp(db, isp_id)
    error = _respaldar(isp.id, isp.slug)
    return _ir(f"/isp/{isp_id}", f"Error: {error}" if error else "Respaldo listo")


@app.get("/isp/{isp_id}/respaldos/{nombre}")
def descargar_respaldo(isp_id: int, nombre: str, _csrf_ok: str = Depends(sesion)):
    with Sesion() as db:
        isp = _isp(db, isp_id)
    try:
        ruta = docker_ops.archivo_respaldo(isp.slug, nombre)
    except docker_ops.ErrorDocker as error:
        raise HTTPException(status_code=404, detail=str(error))
    return FileResponse(ruta, media_type="application/x-tar", filename=nombre)


@app.post("/isp/{isp_id}/respaldos/{nombre}/restaurar")
def restaurar_respaldo(isp_id: int, nombre: str, _csrf_ok: str = Depends(sesion), confirmar: str = Form("")):
    with Sesion() as db:
        isp = _isp(db, isp_id)
        if confirmar.strip() != isp.slug:
            return _ir(f"/isp/{isp_id}", f"Error: escribe {isp.slug} para confirmar la restauración")
        with _candado:
            if isp.slug in _ocupados:
                return _ir(f"/isp/{isp_id}", "Error: ya hay un respaldo o restauración en curso")
            _ocupados.add(isp.slug)
        try:
            docker_ops.restaurar(isp.slug, nombre, arrancar=not isp.suspendido)
        except (docker_ops.ErrorDocker, OSError) as error:
            _bitacora(db, isp.id, "restauración falló", f"{nombre}: {error}")
            db.commit()
            return _ir(f"/isp/{isp_id}", f"Error: {error}")
        finally:
            with _candado:
                _ocupados.discard(isp.slug)
        _bitacora(db, isp.id, "restaurado", f"{nombre} (antes se respaldó el estado previo)")
        db.commit()
    return _ir(f"/isp/{isp_id}", f"Restaurado desde {nombre}")


@app.get("/caddy/permitir")
def permitir_certificado(domain: str = ""):
    """Caddy pregunta antes de sacar un certificado: solo para ISP dados de alta."""
    with Sesion() as db:
        existe = db.scalar(select(Isp.id).where(Isp.dominio == domain.strip().lower()))
    if not existe:
        raise HTTPException(status_code=404)
    return {"ok": True}


@app.get("/salud")
def salud():
    return {"status": "ok"}
