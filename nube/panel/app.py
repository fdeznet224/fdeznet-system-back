"""Panel central de la nube: solo para el dueño.

Da de alta a cada ISP con su subdominio, lleva su mensualidad, lo suspende o
reactiva y le aplica actualizaciones. Corre aparte de las instancias.
"""
import hashlib
import hmac
import logging
import os
import secrets
import shutil
import subprocess
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from passlib.hash import pbkdf2_sha256
from sqlalchemy import func, select

from nube.panel import docker_ops, drive
from nube.panel.cuentas import (
    PRIMER_PUERTO_WEB,
    PRIMER_PUERTO_WG,
    SLUG,
    aviso_al_isp,
    debe_suspenderse,
    dia_de_corte,
    estado_cuenta,
    estado_respaldo,
    nuevo_vencimiento,
    segundos_hasta,
    siguiente_puerto,
)
from nube.panel.db import Bitacora, DriveIsp, Isp, Pago, Respaldo, Sesion, crear_tablas

USUARIO = os.getenv("PANEL_USUARIO", "dueno")
CLAVE_HASH = os.getenv("PANEL_CLAVE_HASH", "")
SECRETO = os.getenv("PANEL_SECRETO", "")
DOMINIO_BASE = os.getenv("PANEL_DOMINIO_BASE", "fdezpay.com")
DIAS_GRACIA = int(os.getenv("PANEL_DIAS_GRACIA", "5"))
COOKIE = "panel_sesion"
HORAS_SESION = 12
COOKIE_SEGURA = os.getenv("PANEL_COOKIE_SEGURA", "1") == "1"
HORA_RESPALDO = os.getenv("PANEL_HORA_RESPALDO", "03:00")  # vacío: sin respaldo diario
HORA_CORTE = os.getenv("PANEL_HORA_CORTE", "09:00")  # vacío: sin corte automático
DIAS_PRORROGA = int(os.getenv("PANEL_DIAS_PRORROGA", "3"))
DIAS_AVISO = int(os.getenv("PANEL_DIAS_AVISO", "5"))  # cuántos días antes de vencer se le avisa al ISP
CONTACTO_PAGO = os.getenv("PANEL_CONTACTO_PAGO", "").strip()  # p. ej. "WhatsApp 55 1234 5678"

if not CLAVE_HASH or len(SECRETO) < 32:
    raise RuntimeError("Configura PANEL_CLAVE_HASH y PANEL_SECRETO (32+ caracteres)")

AQUI = Path(__file__).resolve().parent
log = logging.getLogger("panel")


URL_PANEL = f"https://panel.{DOMINIO_BASE}"
REGRESO_DRIVE = f"{URL_PANEL}/drive/regreso"


def _enlace(slug: str) -> dict[str, str]:
    """Lo que la API de un ISP necesita para pedirle cosas al panel (su Drive)."""
    return {"NUBE_PANEL_URL": URL_PANEL, "NUBE_PANEL_SECRETO": drive.secreto_isp(SECRETO, slug)}


def _asegurar_enlaces() -> None:
    with Sesion() as db:
        slugs = list(db.scalars(select(Isp.slug)))
    for slug in slugs:
        try:
            if docker_ops.asegurar_enlace(slug, {"NUBE_ISP": slug, **_enlace(slug)}):
                log.warning("%s: api.env con enlace al panel; recrea su API para que lo tome", slug)
        except OSError:
            log.exception("No se pudo revisar el api.env de %s", slug)


@asynccontextmanager
async def ciclo_de_vida(_app: FastAPI):
    _asegurar_enlaces()
    if HORA_RESPALDO:
        threading.Thread(target=_respaldos_diarios, daemon=True, name="respaldos").start()
    if HORA_CORTE:
        threading.Thread(target=_cortes_automaticos, daemon=True, name="cortes").start()
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


def _corte(isp: Isp) -> date | None:
    """Día en que el panel lo suspenderá solo; None si no aplica."""
    if isp.suspendido or not isp.corte_automatico or not HORA_CORTE:
        return None
    return dia_de_corte(isp.pagado_hasta, DIAS_GRACIA, isp.prorroga_hasta)


def _contenedores() -> tuple[dict, dict, str | None]:
    try:
        return docker_ops.estado_contenedores(), docker_ops.uso_memoria(), None
    except (docker_ops.ErrorDocker, OSError) as error:
        return {}, {}, f"No se pudo consultar Docker: {error}"


def _recursos_vps() -> dict:
    """RAM, disco y carga de la VPS (el panel ve el /proc y el disco del anfitrión)."""
    datos = {"ram_total": 0.0, "ram_usada": 0.0, "disco_total": 0.0, "disco_usado": 0.0,
             "carga": None, "nucleos": os.cpu_count() or 1}
    try:
        info = {}
        for linea in Path("/proc/meminfo").read_text().splitlines():
            clave, valor = linea.split(":", 1)
            info[clave] = int(valor.split()[0]) / 1048576  # kB → GB
        datos["ram_total"] = info["MemTotal"]
        datos["ram_usada"] = info["MemTotal"] - info.get("MemAvailable", info.get("MemFree", 0))
    except (OSError, KeyError, ValueError):
        pass
    try:
        disco = shutil.disk_usage(docker_ops.NUBE_DIR)
        datos["disco_total"], datos["disco_usado"] = disco.total / 1e9, disco.used / 1e9
    except OSError:
        pass
    try:
        datos["carga"] = os.getloadavg()[0]
    except OSError:
        pass
    return datos


def _pendientes(filas: list[dict], hoy: date) -> list[dict]:
    """Lo que pide al dueño hacer algo, lo más urgente primero."""
    avisos = []
    for f in filas:
        isp, estado, corte = f["isp"], f["estado"], f["corte"]
        enlace = f"/isp/{isp.id}"
        if isp.suspendido and isp.suspension_automatica:
            avisos.append({"orden": 0, "tono": "rojo", "isp": isp.nombre, "enlace": enlace,
                           "texto": "Suspendido por falta de pago; se reactiva solo al registrar su pago"})
        elif estado.clave in {"gracia", "vencido"}:
            cuando = "hoy" if corte and corte <= hoy else f"el {corte:%d/%m}" if corte else None
            texto = f"{estado.texto}; se suspende {cuando}" if cuando else f"{estado.texto}; sin corte automático"
            avisos.append({"orden": 1, "tono": "rojo", "isp": isp.nombre, "enlace": enlace, "texto": texto})
        elif estado.clave in {"por_vencer", "sin_pago"}:
            avisos.append({"orden": 2, "tono": "ambar", "isp": isp.nombre, "enlace": enlace, "texto": estado.texto})
        if not isp.suspendido and not f["en_linea"]:
            avisos.append({"orden": 1, "tono": "rojo", "isp": isp.nombre, "enlace": enlace,
                           "texto": "Su sistema está detenido sin estar suspendido"})
        if f["respaldo"].tono in {"rojo", "ambar"}:
            avisos.append({"orden": 3, "tono": f["respaldo"].tono, "isp": isp.nombre, "enlace": enlace,
                           "texto": f"Respaldo: {f['respaldo'].texto}"})
    # Un renglón por ISP con todos sus avisos; el tono y el orden los pone el más urgente.
    por_isp: dict[str, dict] = {}
    for aviso in sorted(avisos, key=lambda a: a["orden"]):
        grupo = por_isp.setdefault(aviso["enlace"], {**aviso, "textos": []})
        grupo["textos"].append(aviso["texto"])
    return list(por_isp.values())


FILTROS = {
    "todos": lambda f: True,
    "cobrar": lambda f: f["estado"].clave in {"por_vencer", "gracia", "vencido", "sin_pago"},
    "suspendidos": lambda f: f["isp"].suspendido,
}


@app.get("/", response_class=HTMLResponse)
def inicio(request: Request, msg: str = "", ver: str = "todos", csrf: str = Depends(sesion)):
    hoy = date.today()
    inicio_mes = hoy.replace(day=1)
    with Sesion() as db:
        isps = db.scalars(select(Isp).order_by(Isp.nombre)).all()
        respaldos = {isp.id: _estado_respaldo(db, isp) for isp in isps}
        cobrado = db.scalar(select(func.coalesce(func.sum(Pago.monto), 0)).where(Pago.fecha >= inicio_mes))
        nombres = {isp.id: isp.nombre for isp in isps}
        actividad = [
            {"fecha": b.fecha, "accion": b.accion, "detalle": b.detalle, "isp": nombres.get(b.isp_id),
             "isp_id": b.isp_id}
            for b in db.scalars(select(Bitacora).order_by(Bitacora.id.desc()).limit(8))
        ]
    contenedores, memoria, error = _contenedores()
    filas = []
    for isp in isps:
        servicios = contenedores.get(isp.slug, {})
        filas.append({
            "isp": isp,
            "estado": _estado(isp),
            "corte": _corte(isp),
            "en_linea": servicios.get("api", "").startswith("Up"),
            "ram": sum(mb for nombre, mb in memoria.items() if nombre.startswith(f"{isp.slug}-")),
            "respaldo": respaldos[isp.id],
        })
    activos = [isp for isp in isps if not isp.suspendido]
    resumen = {
        "total": len(isps),
        "activos": len(activos),
        "suspendidos": len(isps) - len(activos),
        "en_linea": sum(1 for f in filas if f["en_linea"]),
        "mensual": sum((isp.precio_mensual for isp in activos), Decimal(0)),
        "cobrado": Decimal(cobrado or 0),
        "por_cobrar": sum(1 for f in filas if FILTROS["cobrar"](f)),
        "ram_isp": sum(memoria.values()) / 1024,
    }
    ver = ver if ver in FILTROS else "todos"
    return _render(
        request, "inicio.html", csrf, filas=[f for f in filas if FILTROS[ver](f)], resumen=resumen,
        pendientes=_pendientes(filas, hoy), vps=_recursos_vps(), actividad=actividad, ver=ver,
        hoy=hoy, hora_corte=HORA_CORTE, dias_gracia=DIAS_GRACIA, msg=msg, error=error,
    )


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
                whatsapp=isp.whatsapp, version=isp.version, enlace=_enlace(isp.slug),
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
        cuenta_drive = db.scalar(select(DriveIsp).where(DriveIsp.isp_id == isp_id))
    try:
        en_drive = drive.listar(isp.slug)
        error_drive = None
    except (docker_ops.ErrorDocker, OSError, ValueError) as fallo:
        en_drive, error_drive = [], str(fallo)
    locales = {r["nombre"] for r in docker_ops.listar_respaldos(isp.slug)}
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
        request, "detalle.html", csrf, isp=isp, estado=_estado(isp), pagos=pagos, corte=_corte(isp),
        prorroga=date.today() + timedelta(days=DIAS_PRORROGA), hora_corte=HORA_CORTE,
        debe=debe_suspenderse(isp.pagado_hasta, False, isp.corte_automatico, None, date.today(), DIAS_GRACIA),
        bitacora=bitacora, servicios=servicios, versiones=versiones, msg=msg, error=error,
        hoy=date.today(), respaldo=respaldo, respaldos=docker_ops.listar_respaldos(isp.slug),
        hora_respaldo=HORA_RESPALDO, cuenta_drive=cuenta_drive, error_drive=error_drive,
        en_drive=[r for r in en_drive if r["nombre"] not in locales],
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
        reactivado = False
        if isp.suspendido and isp.suspension_automatica and not debe_suspenderse(
            cubre, False, True, isp.prorroga_hasta, date.today(), DIAS_GRACIA
        ):
            try:
                docker_ops.encender(isp.slug)
                isp.suspendido, isp.suspension_automatica, reactivado = False, False, True
                _bitacora(db, isp.id, "reactivado por pago")
            except (docker_ops.ErrorDocker, OSError) as error:
                db.commit()
                return _ir(f"/isp/{isp_id}", f"Error: pago registrado, pero no se pudo reactivar: {error}")
        db.commit()
    mensaje = f"Pago registrado: cubre hasta {cubre:%d/%m/%Y}"
    return _ir(f"/isp/{isp_id}", f"{mensaje}. ISP reactivado" if reactivado else mensaje)


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
    corte_automatico: bool = Form(False),
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
        if corte_automatico != isp.corte_automatico:
            _bitacora(db, isp.id, "corte automático", "activado" if corte_automatico else "desactivado")
            isp.corte_automatico = corte_automatico
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


@app.post("/isp/{isp_id}/restablecer-admin", response_class=HTMLResponse)
def restablecer_admin(request: Request, isp_id: int, csrf: str = Depends(sesion), usuario: str = Form("admin")):
    usuario = usuario.strip() or "admin"
    clave = secrets.token_urlsafe(12).replace("-", "x").replace("_", "y")
    with Sesion() as db:
        isp = _isp(db, isp_id)
        if isp.suspendido:
            return _ir(f"/isp/{isp_id}", "Error: reactiva el ISP antes de restablecer la contraseña")
        try:
            docker_ops.restablecer_admin(isp.slug, usuario, clave)
        except (docker_ops.ErrorDocker, OSError) as error:
            return _ir(f"/isp/{isp_id}", f"Error: {error}")
        _bitacora(db, isp.id, "contraseña restablecida", f"usuario {usuario}")
        db.commit()
    # Como en el alta: se muestra una sola vez y no se guarda en el panel.
    return _render(request, "creado.html", csrf, isp=isp, clave=clave, usuario=usuario, restablecida=True)


@app.post("/isp/{isp_id}/suspender")
def suspender(isp_id: int, _csrf_ok: str = Depends(sesion)):
    with Sesion() as db:
        isp = _isp(db, isp_id)
        try:
            docker_ops.detener(isp.slug)
        except (docker_ops.ErrorDocker, OSError) as error:
            return _ir(f"/isp/{isp_id}", f"Error: {error}")
        isp.suspendido, isp.suspension_automatica = True, False
        _bitacora(db, isp.id, "suspendido")
        db.commit()
    return _ir(f"/isp/{isp_id}", "ISP suspendido; sus datos se conservan")


@app.post("/isp/{isp_id}/reactivar")
def reactivar(isp_id: int, _csrf_ok: str = Depends(sesion), prorroga: str = Form("")):
    hoy = date.today()
    with Sesion() as db:
        isp = _isp(db, isp_id)
        # Si todavía debe, se le da una prórroga; si no, el corte lo volvería a suspender.
        debe = debe_suspenderse(isp.pagado_hasta, False, isp.corte_automatico, None, hoy, DIAS_GRACIA)
        hasta = None
        if debe:
            hasta = date.fromisoformat(prorroga) if prorroga else hoy + timedelta(days=DIAS_PRORROGA)
            if hasta < hoy:
                return _ir(f"/isp/{isp_id}", "Error: la prórroga no puede ser de una fecha pasada")
        try:
            docker_ops.encender(isp.slug)
        except (docker_ops.ErrorDocker, OSError) as error:
            return _ir(f"/isp/{isp_id}", f"Error: {error}")
        isp.suspendido, isp.suspension_automatica = False, False
        if hasta:
            isp.prorroga_hasta = hasta
        _bitacora(db, isp.id, "reactivado", f"Prórroga hasta {hasta:%d/%m/%Y}" if hasta else "")
        db.commit()
    if hasta:
        return _ir(f"/isp/{isp_id}", f"ISP reactivado con prórroga hasta {hasta:%d/%m/%Y}")
    return _ir(f"/isp/{isp_id}", "ISP reactivado")


# --- Corte automático -----------------------------------------------------

def _revisar_cortes(hoy: date) -> list[str]:
    """Suspende a los ISP que pasaron la gracia sin pagar; devuelve sus slugs."""
    suspendidos = []
    with Sesion() as db:
        for isp in db.scalars(select(Isp).where(Isp.suspendido.is_(False))):
            if not debe_suspenderse(isp.pagado_hasta, isp.suspendido, isp.corte_automatico,
                                    isp.prorroga_hasta, hoy, DIAS_GRACIA):
                continue
            try:
                docker_ops.detener(isp.slug)
            except (docker_ops.ErrorDocker, OSError) as error:
                log.error("No se pudo suspender a %s: %s", isp.slug, error)
                _bitacora(db, isp.id, "corte falló", str(error)[-300:])
                continue
            isp.suspendido, isp.suspension_automatica = True, True
            _bitacora(db, isp.id, "suspendido automático",
                      f"Pagado hasta {isp.pagado_hasta:%d/%m/%Y}; terminó la gracia de {DIAS_GRACIA} días")
            suspendidos.append(isp.slug)
        db.commit()
    return suspendidos


def _cortes_automaticos() -> None:
    """Hilo del panel: cada 15 min, ya pasada la HORA_CORTE, suspende a los vencidos.

    Revisar seguido (y no solo a la hora exacta) cubre los reinicios del panel.
    """
    horas, minutos = (int(parte) for parte in HORA_CORTE.split(":"))
    while True:
        ahora = datetime.now()
        if (ahora.hour, ahora.minute) >= (horas, minutos):
            try:
                for slug in _revisar_cortes(ahora.date()):
                    log.warning("%s suspendido por falta de pago", slug)
            except Exception:  # noqa: BLE001 - el hilo no debe morir
                log.exception("Falló la revisión de cortes")
        time.sleep(15 * 60)


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
        datos = docker_ops.respaldar(slug, drive.ajustes_respaldo(slug) if drive.conectado(slug) else None)
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


@app.post("/isp/{isp_id}/drive/{nombre}/traer")
def traer_de_drive(isp_id: int, nombre: str, _csrf_ok: str = Depends(sesion)):
    with Sesion() as db:
        isp = _isp(db, isp_id)
    try:
        drive.traer(isp.slug, nombre)
    except (docker_ops.ErrorDocker, OSError, subprocess.TimeoutExpired) as error:
        return _ir(f"/isp/{isp_id}", f"Error: {error}")
    return _ir(f"/isp/{isp_id}", f"{nombre} ya está en la VPS; ahí se puede restaurar")


# --- Google Drive del ISP (lo pide la API del ISP con firma) -------------------

def _isp_firmado(db, isp: str, accion: str, vence: str, firma: str) -> Isp:
    registro = db.scalar(select(Isp).where(Isp.slug == isp))
    if not registro or not drive.firma_valida(SECRETO, isp, accion, vence, firma):
        raise HTTPException(status_code=403, detail="Enlace vencido; vuelve a intentarlo desde tu panel")
    return registro


@app.get("/cuenta/estado")
def cuenta_estado(isp: str = "", vence: str = "", firma: str = ""):
    """El admin del ISP pregunta si tiene que mostrar el aviso de pago."""
    with Sesion() as db:
        registro = _isp_firmado(db, isp, "cuenta", vence, firma)
        aviso = aviso_al_isp(registro.pagado_hasta, registro.suspendido, _corte(registro), date.today(),
                             HORA_CORTE, DIAS_AVISO, CONTACTO_PAGO)
    return {"aviso": aviso}


def _volver_al_isp(isp: Isp, resultado: str) -> RedirectResponse:
    destino = f"https://{isp.dominio}/admin/configuracion/almacenamiento?{urlencode({'drive': resultado})}"
    return RedirectResponse(destino, status_code=303)


@app.get("/drive/conectar")
def drive_conectar(isp: str = "", vence: str = "", firma: str = "", correo: str = ""):
    with Sesion() as db:
        registro = _isp_firmado(db, isp, "conectar", vence, firma)
    if not drive.configurado():
        return _volver_al_isp(registro, "El respaldo en Drive todavía no está disponible; avisa a soporte")
    estado = jwt.encode({"isp": registro.slug, "exp": int(time.time()) + drive.VIGENCIA_ENLACE}, SECRETO, algorithm="HS256")
    return RedirectResponse(drive.url_autorizar(REGRESO_DRIVE, estado, correo.strip()), status_code=303)


@app.get("/drive/regreso")
def drive_regreso(state: str = "", code: str = "", error: str = ""):
    try:
        slug = jwt.decode(state, SECRETO, algorithms=["HS256"])["isp"]
    except (jwt.PyJWTError, KeyError):
        raise HTTPException(status_code=400, detail="Enlace vencido; vuelve a conectar desde tu panel")
    with Sesion() as db:
        isp = db.scalar(select(Isp).where(Isp.slug == slug))
        if not isp:
            raise HTTPException(status_code=404)
        if error or not code:
            return _volver_al_isp(isp, "No se conectó: no se dio permiso en Google")
        try:
            token, correo = drive.canjear_codigo(code, REGRESO_DRIVE)
            drive.guardar_conexion(slug, token)
        except (drive.ErrorDrive, httpx.HTTPError, OSError, subprocess.TimeoutExpired) as fallo:
            _bitacora(db, isp.id, "drive falló", str(fallo))
            db.commit()
            return _volver_al_isp(isp, f"No se conectó: {fallo}")
        cuenta = db.scalar(select(DriveIsp).where(DriveIsp.isp_id == isp.id)) or DriveIsp(isp_id=isp.id)
        cuenta.correo, cuenta.conectado = correo or "cuenta de Google", datetime.now()
        db.add(cuenta)
        _bitacora(db, isp.id, "drive conectado", cuenta.correo)
        db.commit()
    # Primer respaldo de una vez: así el ISP ve que ya quedó.
    threading.Thread(target=_respaldar, args=(isp.id, slug), daemon=True).start()
    return _volver_al_isp(isp, "ok")


def _estado_drive(db, isp: Isp) -> dict:
    cuenta = db.scalar(select(DriveIsp).where(DriveIsp.isp_id == isp.id))
    ultimo = db.scalar(
        select(Respaldo).where(Respaldo.isp_id == isp.id).order_by(Respaldo.id.desc()).limit(1)
    )
    return {
        "disponible": drive.configurado(),
        "conectado": bool(cuenta) and drive.conectado(isp.slug),
        "correo": cuenta.correo if cuenta else None,
        "conectado_desde": cuenta.conectado.isoformat() if cuenta else None,
        "carpeta": drive.CARPETA,
        "dias": int(drive.DIAS_EN_DRIVE),
        "hora": HORA_RESPALDO or None,
        "en_curso": isp.slug in _ocupados,
        "ultimo": {
            "fecha": ultimo.fecha.isoformat(),
            "tamano": ultimo.tamano,
            # REMOTO=ok cuando subió; si no, el error (de rclone o del respaldo mismo).
            "subido": ultimo.remoto == "ok",
            "error": ultimo.error or (ultimo.remoto if ultimo.remoto not in {"ok", "no", None} else None),
        } if ultimo else None,
    }


@app.get("/drive/estado")
def drive_estado(isp: str = "", vence: str = "", firma: str = ""):
    with Sesion() as db:
        return _estado_drive(db, _isp_firmado(db, isp, "estado", vence, firma))


@app.post("/drive/desconectar")
def drive_desconectar(isp: str = "", vence: str = "", firma: str = ""):
    with Sesion() as db:
        registro = _isp_firmado(db, isp, "desconectar", vence, firma)
        drive.desconectar(registro.slug)
        cuenta = db.scalar(select(DriveIsp).where(DriveIsp.isp_id == registro.id))
        if cuenta:
            db.delete(cuenta)
            _bitacora(db, registro.id, "drive desconectado", cuenta.correo)
        db.commit()
        return _estado_drive(db, registro)


@app.post("/drive/respaldar")
def drive_respaldar(isp: str = "", vence: str = "", firma: str = ""):
    with Sesion() as db:
        registro = _isp_firmado(db, isp, "respaldar", vence, firma)
    if registro.slug in _ocupados:
        return JSONResponse({"detail": "Ya se está respaldando"}, status_code=409)
    threading.Thread(target=_respaldar, args=(registro.id, registro.slug), daemon=True).start()
    with Sesion() as db:
        estado = _estado_drive(db, registro)
    return {**estado, "en_curso": True}


@app.get("/caddy/permitir")
def permitir_certificado(domain: str = ""):
    """Caddy pregunta antes de sacar un certificado: solo para ISP dados de alta."""
    with Sesion() as db:
        existe = db.scalar(select(Isp.id).where(Isp.dominio == domain.strip().lower()))
    if not existe:
        raise HTTPException(status_code=404)
    return {"ok": True}


@app.get("/privacidad", response_class=HTMLResponse)
def privacidad(request: Request):
    return _render(request, "privacidad.html")


@app.get("/terminos", response_class=HTMLResponse)
def terminos(request: Request):
    return _render(request, "terminos.html")


@app.get("/salud")
def salud():
    return {"status": "ok"}
