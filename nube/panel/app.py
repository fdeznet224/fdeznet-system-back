"""Panel central de la nube: solo para el dueño.

Da de alta a cada ISP con su subdominio, lleva su mensualidad, lo suspende o
reactiva y le aplica actualizaciones. Corre aparte de las instancias.
"""
import hashlib
import hmac
import logging
import os
import re
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

from nube.panel import docker_ops, drive, whatsapp
from nube.panel.cuentas import (
    PRIMER_PUERTO_WEB,
    PRIMER_PUERTO_WG,
    RESERVADOS,
    SLUG,
    aviso_al_isp,
    debe_suspenderse,
    dia_de_corte,
    estado_cuenta,
    estado_demo,
    estado_respaldo,
    etapas_demo,
    limpiar_texto,
    mensaje_bienvenida,
    mensaje_demo_por_terminar,
    mensaje_demo_se_borra,
    mensaje_demo_terminado,
    nuevo_vencimiento,
    segundos_hasta,
    siguiente_paso_demo,
    siguiente_puerto,
    slug_para,
    telefono_mx,
)
from nube.panel.db import Bitacora, DriveIsp, Isp, Liberacion, Pago, Respaldo, Sesion, crear_tablas

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
# Demos que se piden solos en la página de registro (wispay.click).
DEMO_DIAS = int(os.getenv("PANEL_DEMO_DIAS", "15"))
DEMO_GRACIA = int(os.getenv("PANEL_DEMO_GRACIA", "15"))  # días suspendido antes de borrarse
DEMOS_MAX = int(os.getenv("PANEL_DEMOS_MAX", "20"))
DEMO_RAM_MINIMA_GB = float(os.getenv("PANEL_DEMO_RAM_MINIMA_GB", "1.5"))
DEMO_LIMITE_CLIENTES = int(os.getenv("PANEL_DEMO_LIMITE_CLIENTES", "25"))
DEMO_LIMITE_ROUTERS = int(os.getenv("PANEL_DEMO_LIMITE_ROUTERS", "3"))
REGISTROS_POR_IP = int(os.getenv("PANEL_REGISTROS_POR_IP", "3"))  # por día

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
    threading.Thread(target=_tareas_del_dia, daemon=True, name="cortes-y-demos").start()
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
    if isp.es_demo and isp.demo_hasta:
        return estado_demo(isp.demo_hasta, isp.suspendido, date.today(), DEMO_GRACIA)
    return estado_cuenta(isp.pagado_hasta, isp.suspendido, date.today(), dias_gracia=DIAS_GRACIA)


def _pasos(isp: Isp) -> set[str]:
    return set(filter(None, (isp.demo_pasos or "").split(",")))


def _marcar_paso(isp: Isp, paso: str, hecho: bool = True) -> None:
    pasos = _pasos(isp)
    pasos.add(paso) if hecho else pasos.discard(paso)
    isp.demo_pasos = ",".join(sorted(pasos)) or None


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
        pasos = _pasos(isp)
        if "creando" in pasos:
            avisos.append({"orden": 3, "tono": "gris", "isp": isp.nombre, "enlace": enlace,
                           "texto": "Demo nuevo: preparando su sistema…"})
            continue
        if "sin_envio" in pasos:
            avisos.append({"orden": 0, "tono": "rojo", "isp": isp.nombre, "enlace": enlace,
                           "texto": "No se le pudo mandar su acceso por WhatsApp; usa Reenviar acceso"})
        if isp.es_demo:
            if estado.clave == "demo_por_vencer":
                avisos.append({"orden": 2, "tono": "ambar", "isp": isp.nombre, "enlace": enlace,
                               "texto": f"{estado.texto}; buen momento para llamarle ({isp.contacto or ''})"})
            elif estado.clave == "demo_fin":
                avisos.append({"orden": 3, "tono": "gris", "isp": isp.nombre, "enlace": enlace, "texto": estado.texto})
        elif isp.suspendido and isp.suspension_automatica:
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
    "demos": lambda f: f["isp"].es_demo,
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
        por_contactar = [
            {"fecha": b.fecha, "accion": b.accion, "detalle": b.detalle,
             "telefono": (re.search(r"\b52\d{10}\b", b.detalle or "") or [None])[0]}
            for b in db.scalars(
                select(Bitacora).where(Bitacora.accion.in_(ACCIONES_POR_CONTACTAR),
                                       Bitacora.fecha >= datetime.now() - timedelta(days=30))
                .order_by(Bitacora.id.desc()).limit(8))
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
    activos = [isp for isp in isps if not isp.suspendido and not isp.es_demo]
    resumen = {
        "total": sum(1 for isp in isps if not isp.es_demo),
        "activos": len(activos),
        "suspendidos": sum(1 for isp in isps if isp.suspendido and not isp.es_demo),
        "demos": sum(1 for isp in isps if isp.es_demo and not isp.suspendido),
        "en_linea": sum(1 for f in filas if f["en_linea"] and not f["isp"].es_demo),
        "mensual": sum((isp.precio_mensual for isp in activos), Decimal(0)),
        "cobrado": Decimal(cobrado or 0),
        "por_cobrar": sum(1 for f in filas if FILTROS["cobrar"](f)),
        "ram_isp": sum(memoria.values()) / 1024,
    }
    ver = ver if ver in FILTROS else "todos"
    return _render(
        request, "inicio.html", csrf, filas=[f for f in filas if FILTROS[ver](f)], resumen=resumen,
        pendientes=_pendientes(filas, hoy), vps=_recursos_vps(), actividad=actividad, ver=ver,
        whatsapp_ventas=whatsapp.estado(), demos_max=DEMOS_MAX, demo_dias=DEMO_DIAS, por_contactar=por_contactar,
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
    if slug in RESERVADOS:
        raise HTTPException(status_code=400, detail="Ese subdominio está reservado")
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
        demo=etapas_demo(isp.demo_hasta, DEMO_GRACIA) if isp.es_demo and isp.demo_hasta else None,
        pasos=_pasos(isp),
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
        if isp.es_demo:
            # Con su primer pago deja de ser demo: ya no se borra y entra al corte normal.
            isp.es_demo, isp.demo_pasos = False, None
            _bitacora(db, isp.id, "demo convertido en cliente")
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


def _tareas_del_dia() -> None:
    """Hilo del panel: cada 15 min, ya pasada la HORA_CORTE, suspende a los vencidos
    y avanza los demos (avisos, fin, borrado).

    Revisar seguido (y no solo a la hora exacta) cubre los reinicios del panel.
    """
    horas, minutos = (int(parte) for parte in (HORA_CORTE or "09:00").split(":"))
    while True:
        ahora = datetime.now()
        if (ahora.hour, ahora.minute) >= (horas, minutos):
            try:
                if HORA_CORTE:
                    for slug in _revisar_cortes(ahora.date()):
                        log.warning("%s suspendido por falta de pago", slug)
                _revisar_demos(ahora.date())
            except Exception:  # noqa: BLE001 - el hilo no debe morir
                log.exception("Falló la revisión de cortes y demos")
        time.sleep(15 * 60)


# --- Demos ------------------------------------------------------------------

def _avisar(isp: Isp, mensaje: str) -> bool:
    """Manda un WhatsApp a quien pidió el demo; si no se puede, lo deja en la bitácora."""
    if not isp.telefono:
        return False
    try:
        whatsapp.enviar(isp.telefono, mensaje)
        return True
    except whatsapp.ErrorWhatsApp as error:
        log.warning("No se pudo avisar a %s: %s", isp.slug, error)
        return False


def _revisar_demos(hoy: date) -> list[tuple[str, str]]:
    """Avanza cada demo un paso a la vez; devuelve (slug, paso) de lo que hizo."""
    hechos = []
    with Sesion() as db:
        demos = db.scalars(select(Isp).where(Isp.es_demo.is_(True), Isp.demo_hasta.isnot(None))).all()
        for isp in demos:
            if "creando" in _pasos(isp):
                continue
            paso = siguiente_paso_demo(isp.demo_hasta, _pasos(isp), hoy, DEMO_GRACIA)
            if not paso:
                continue
            fechas = etapas_demo(isp.demo_hasta, DEMO_GRACIA)
            nombre, empresa = (isp.contacto or "").split(" ")[0] or "", isp.nombre
            try:
                if paso == "aviso":
                    _avisar(isp, mensaje_demo_por_terminar(nombre, empresa, f"https://{isp.dominio}", isp.demo_hasta))
                elif paso == "fin":
                    if not isp.suspendido:
                        docker_ops.detener(isp.slug)
                        isp.suspendido, isp.suspension_automatica = True, True
                    _avisar(isp, mensaje_demo_terminado(nombre, empresa, fechas["borrar"]))
                    _bitacora(db, isp.id, "demo terminado", f"Se borra el {fechas['borrar']:%d/%m/%Y}")
                elif paso == "aviso_borrar":
                    _avisar(isp, mensaje_demo_se_borra(nombre, empresa, fechas["borrar"]))
                elif paso == "borrar":
                    docker_ops.borrar(isp.slug)
                    _bitacora(db, None, "demo borrado",
                              f"{isp.nombre} ({isp.dominio}) · {isp.contacto or ''} {isp.telefono or ''}".strip())
                    db.delete(isp)
                    db.commit()
                    hechos.append((isp.slug, paso))
                    continue
            except (docker_ops.ErrorDocker, OSError) as error:
                log.error("Demo %s, paso %s: %s", isp.slug, paso, error)
                _bitacora(db, isp.id, f"demo: {paso} falló", str(error)[-300:])
                db.commit()
                continue
            _marcar_paso(isp, paso)
            db.commit()
            hechos.append((isp.slug, paso))
    return hechos


_candado_demos = threading.Lock()
_registros_ip: dict[str, deque] = defaultdict(deque)


def _ram_libre_gb() -> float:
    try:
        for linea in Path("/proc/meminfo").read_text().splitlines():
            if linea.startswith("MemAvailable:"):
                return int(linea.split()[1]) / 1048576
    except (OSError, ValueError):
        pass
    return 99.0  # sin /proc (pruebas): no se limita por RAM


def _orden_version(version: str | None) -> tuple:
    return tuple(int(x) if x.isdigit() else -1 for x in (version or "").split("."))


def _liberada(db) -> Liberacion | None:
    """La versión que se les ofrece hoy a los ISP (la última liberada)."""
    return db.scalar(select(Liberacion).order_by(Liberacion.id.desc()).limit(1))


def _ultima_version() -> str:
    """Con la que nacen los demos: la liberada; si nunca se ha liberado, la más nueva construida."""
    try:
        versiones = docker_ops.versiones_disponibles()
    except (docker_ops.ErrorDocker, OSError):
        versiones = []
    with Sesion() as db:
        liberada = _liberada(db)
    if liberada and liberada.version in versiones:
        return liberada.version
    if not versiones:
        return "local"
    return max(versiones, key=_orden_version)


def _crear_demo(isp_id: int, clave_nombre: str) -> None:
    """Hilo: crea la instancia del demo y le manda su acceso por WhatsApp."""
    with Sesion() as db:
        isp = db.get(Isp, isp_id)
        datos = dict(slug=isp.slug, web=isp.web_port, wg=isp.wg_port, dominio=isp.dominio, version=isp.version,
                     nombre=isp.nombre, telefono=isp.telefono, correo=isp.correo or "")
    try:
        clave = docker_ops.crear(
            datos["slug"], datos["web"], datos["wg"], datos["dominio"],
            limite_clientes=DEMO_LIMITE_CLIENTES, limite_routers=DEMO_LIMITE_ROUTERS, whatsapp=False,
            version=datos["version"], enlace=_enlace(datos["slug"]),
            marca={"MARCA_NOMBRE": datos["nombre"], "MARCA_TELEFONO": datos["telefono"][2:],
                   "MARCA_CORREO": datos["correo"]},
        )
    except (docker_ops.ErrorDocker, OSError) as error:
        log.error("No se pudo crear el demo %s: %s", datos["slug"], error)
        with Sesion() as db:
            isp = db.get(Isp, isp_id)
            _bitacora(db, None, "demo no se pudo crear",
                      f"{isp.nombre} · {isp.contacto} {isp.telefono} · {str(error)[-200:]}")
            db.delete(isp)
            db.commit()
        try:
            docker_ops.borrar(datos["slug"])
        except (docker_ops.ErrorDocker, OSError):
            pass
        try:
            whatsapp.enviar(datos["telefono"], f"Hola {clave_nombre}, tuvimos un problema al preparar tu demo de "
                            "WisPay. Ya nos avisaron y te escribimos en cuanto quede listo.")
        except whatsapp.ErrorWhatsApp:
            pass
        return
    with Sesion() as db:
        isp = db.get(Isp, isp_id)
        _marcar_paso(isp, "creando", False)
        mensaje = mensaje_bienvenida(clave_nombre, isp.nombre, f"https://{isp.dominio}", "admin", clave, isp.demo_hasta)
        if _avisar(isp, mensaje):
            _bitacora(db, isp.id, "demo creado", f"Acceso enviado por WhatsApp a {isp.telefono}")
        else:
            # La contraseña no se guarda: con Restablecer contraseña se le manda otra.
            _marcar_paso(isp, "sin_envio")
            _bitacora(db, isp.id, "demo creado", "No se le pudo mandar su acceso por WhatsApp")
        db.commit()


@app.post("/api/registro")
async def registro(request: Request):
    """Formulario público de wispay.click: pide un demo gratis."""
    try:
        datos = await request.json()
    except ValueError:
        return JSONResponse({"ok": False, "error": "Formulario inválido"}, status_code=400)
    if not isinstance(datos, dict):
        return JSONResponse({"ok": False, "error": "Formulario inválido"}, status_code=400)
    if str(datos.get("sitio_web") or "").strip():  # campo trampa para robots
        return {"ok": True, "mensaje": "Recibido"}
    empresa = limpiar_texto(str(datos.get("empresa") or ""), 60)
    nombre = limpiar_texto(str(datos.get("nombre") or ""), 60)
    telefono = telefono_mx(str(datos.get("telefono") or ""))
    correo = limpiar_texto(str(datos.get("correo") or ""), 120).replace(" ", "")
    clientes = limpiar_texto(str(datos.get("clientes") or ""), 20)
    if len(empresa) < 2 or len(nombre) < 2:
        return JSONResponse({"ok": False, "error": "Escribe el nombre de tu empresa y el tuyo"}, status_code=400)
    if not telefono:
        return JSONResponse({"ok": False, "error": "Escribe tu WhatsApp a 10 dígitos"}, status_code=400)
    if correo and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}", correo):
        return JSONResponse({"ok": False, "error": "Revisa tu correo"}, status_code=400)

    ip = request.client.host if request.client else "?"
    cola = _registros_ip[ip]
    while cola and cola[0] < time.monotonic() - 86400:
        cola.popleft()
    if len(cola) >= REGISTROS_POR_IP:
        return JSONResponse({"ok": False, "error": "Ya pediste varios demos hoy; escríbenos por WhatsApp"}, status_code=429)

    with _candado_demos:
        with Sesion() as db:
            previo = db.scalar(select(Isp).where(Isp.telefono == telefono, Isp.es_demo.is_(True)))
            if previo:
                return {"ok": True, "existente": True, "direccion": f"https://{previo.dominio}",
                        "mensaje": "Ese WhatsApp ya tiene un demo. Si no encuentras tu contraseña, "
                                   "responde al mensaje que te mandamos y te ayudamos."}
            demos = db.scalar(select(func.count()).select_from(Isp).where(
                Isp.es_demo.is_(True), Isp.suspendido.is_(False))) or 0
            if demos >= DEMOS_MAX or _ram_libre_gb() < DEMO_RAM_MINIMA_GB or not whatsapp.estado()["conectado"]:
                # Sin lugar (o sin WhatsApp para mandarle su acceso): queda anotado para llamarle.
                _bitacora(db, None, "registro en espera",
                          f"{empresa} · {nombre} · {telefono} {correo} · {clientes} clientes".strip())
                db.commit()
                return {"ok": True, "espera": True,
                        "mensaje": "¡Gracias! En este momento estamos preparando lugar; te escribimos por "
                                   "WhatsApp en cuanto tu demo esté listo."}
            ocupados = set(db.scalars(select(Isp.slug)))
            ocupados |= {p.name for p in (docker_ops.NUBE_DIR / "instancias").glob("*") if p.is_dir()}
            usados = db.execute(select(Isp.web_port, Isp.wg_port)).all()
            web_disco, wg_disco = docker_ops.puertos_en_uso()
            slug = slug_para(empresa, ocupados)
            isp = Isp(
                nombre=empresa, slug=slug, dominio=f"{slug}.{DOMINIO_BASE}", contacto=nombre, telefono=telefono,
                correo=correo or None, plan="Demo", precio_mensual=Decimal(0), es_demo=True,
                demo_hasta=date.today() + timedelta(days=DEMO_DIAS), demo_pasos="creando", registro_ip=ip,
                limite_clientes=DEMO_LIMITE_CLIENTES, limite_routers=DEMO_LIMITE_ROUTERS, whatsapp=False,
                version=_ultima_version(), notas=f"Registro web · {clientes} clientes" if clientes else "Registro web",
                web_port=siguiente_puerto([u.web_port for u in usados] + web_disco, PRIMER_PUERTO_WEB),
                wg_port=siguiente_puerto([u.wg_port for u in usados] + wg_disco, PRIMER_PUERTO_WG),
            )
            db.add(isp)
            db.flush()
            _bitacora(db, isp.id, "registro web", f"{nombre} · {telefono} {correo}".strip())
            db.commit()
            isp_id, direccion = isp.id, f"https://{isp.dominio}"
    cola.append(time.monotonic())
    threading.Thread(target=_crear_demo, args=(isp_id, nombre.split(" ")[0]), daemon=True).start()
    return {"ok": True, "direccion": direccion,
            "mensaje": "¡Listo! En unos minutos te llega por WhatsApp tu acceso."}


_interes_ip: dict[str, deque] = defaultdict(deque)
# Lo que dejan los interesados y conviene que el dueño vea para llamarles.
ACCIONES_POR_CONTACTAR = ("interesado en fichas", "registro en espera", "demo no se pudo crear")


@app.post("/api/interes")
async def interes(request: Request):
    """Formulario público de WisPay Fichas (hotspot): no crea nada, deja el contacto."""
    try:
        datos = await request.json()
    except ValueError:
        datos = None
    if not isinstance(datos, dict):
        return JSONResponse({"ok": False, "error": "Formulario inválido"}, status_code=400)
    if str(datos.get("sitio_web") or "").strip():
        return {"ok": True, "mensaje": "Recibido"}
    empresa = limpiar_texto(str(datos.get("empresa") or ""), 60)
    nombre = limpiar_texto(str(datos.get("nombre") or ""), 60)
    telefono = telefono_mx(str(datos.get("telefono") or ""))
    if len(empresa) < 2 or len(nombre) < 2:
        return JSONResponse({"ok": False, "error": "Escribe el nombre de tu empresa y el tuyo"}, status_code=400)
    if not telefono:
        return JSONResponse({"ok": False, "error": "Escribe tu WhatsApp a 10 dígitos"}, status_code=400)
    ip = request.client.host if request.client else "?"
    cola = _interes_ip[ip]
    while cola and cola[0] < time.monotonic() - 86400:
        cola.popleft()
    if len(cola) >= 5:
        return JSONResponse({"ok": False, "error": "Ya recibimos tus datos; te escribimos pronto"}, status_code=429)
    cola.append(time.monotonic())
    with Sesion() as db:
        _bitacora(db, None, "interesado en fichas", f"{empresa} · {nombre} · {telefono}")
        db.commit()
    try:
        whatsapp.enviar(telefono, f"Hola {nombre.split(' ')[0]} 👋, gracias por tu interés en *WisPay Fichas* para "
                                  f"*{empresa}*. En breve te escribimos con tu cotización.")
    except whatsapp.ErrorWhatsApp:
        pass  # queda en el panel para llamarle
    return {"ok": True, "mensaje": "¡Gracias! Te escribimos por WhatsApp con tu cotización."}


@app.post("/isp/{isp_id}/demo")
def ajustar_demo(isp_id: int, _csrf_ok: str = Depends(sesion), demo_hasta: date = Form(...)):
    """Alargar (o acortar) el demo; si ya estaba en pausa por el fin del demo, se reactiva."""
    with Sesion() as db:
        isp = _isp(db, isp_id)
        if not isp.es_demo:
            raise HTTPException(status_code=400, detail="No es un demo")
        antes = isp.demo_hasta
        isp.demo_hasta = demo_hasta
        isp.demo_pasos = ",".join(sorted(_pasos(isp) & {"sin_envio"})) or None
        if isp.suspendido and isp.suspension_automatica and demo_hasta >= date.today():
            try:
                docker_ops.encender(isp.slug)
            except (docker_ops.ErrorDocker, OSError) as error:
                return _ir(f"/isp/{isp_id}", f"Error: {error}")
            isp.suspendido, isp.suspension_automatica = False, False
        _bitacora(db, isp.id, "demo ajustado", f"{antes:%d/%m/%Y} → {demo_hasta:%d/%m/%Y}" if antes else "")
        db.commit()
    return _ir(f"/isp/{isp_id}", f"Demo hasta {demo_hasta:%d/%m/%Y}")


@app.post("/isp/{isp_id}/demo/reenviar")
def reenviar_acceso(isp_id: int, _csrf_ok: str = Depends(sesion)):
    """Le pone una contraseña nueva al admin del demo y se la manda por WhatsApp."""
    with Sesion() as db:
        isp = _isp(db, isp_id)
        if not isp.telefono:
            return _ir(f"/isp/{isp_id}", "Error: no tiene WhatsApp registrado")
        clave = secrets.token_urlsafe(9)
        try:
            docker_ops.restablecer_admin(isp.slug, "admin", clave)
        except (docker_ops.ErrorDocker, OSError) as error:
            return _ir(f"/isp/{isp_id}", f"Error: {error}")
        hasta = isp.demo_hasta or date.today()
        mensaje = mensaje_bienvenida((isp.contacto or "").split(" ")[0], isp.nombre, f"https://{isp.dominio}",
                                     "admin", clave, hasta)
        if not _avisar(isp, mensaje):
            db.commit()
            return _ir(f"/isp/{isp_id}", "Error: se cambió la contraseña pero no se pudo mandar por WhatsApp")
        _marcar_paso(isp, "sin_envio", False)
        _bitacora(db, isp.id, "acceso reenviado", f"Por WhatsApp a {isp.telefono}")
        db.commit()
    return _ir(f"/isp/{isp_id}", "Acceso nuevo enviado por WhatsApp")


# --- WhatsApp de ventas -------------------------------------------------------

@app.get("/versiones", response_class=HTMLResponse)
def versiones_pagina(request: Request, msg: str = "", csrf: str = Depends(sesion)):
    try:
        construidas = docker_ops.versiones_disponibles()
    except (docker_ops.ErrorDocker, OSError):
        construidas = []
    with Sesion() as db:
        liberada = _liberada(db)
        historial = db.scalars(select(Liberacion).order_by(Liberacion.id.desc()).limit(10)).all()
        isps = db.scalars(select(Isp).order_by(Isp.nombre)).all()
    actual = liberada.version if liberada else None
    pendientes = [i for i in isps if actual and _orden_version(i.version) < _orden_version(actual)]
    return _render(request, "versiones.html", csrf, construidas=construidas, liberada=liberada,
                   historial=historial, isps=isps, pendientes=pendientes, actualizando=_actualizando,
                   orden=_orden_version, msg=msg)


@app.post("/versiones/liberar")
def versiones_liberar(_csrf_ok: str = Depends(sesion), version: str = Form(...), novedades: str = Form("")):
    try:
        construidas = docker_ops.versiones_disponibles()
    except (docker_ops.ErrorDocker, OSError):
        construidas = []
    if version not in construidas:
        return _ir("/versiones", "Error: esa versión no está construida en la VPS")
    with Sesion() as db:
        db.add(Liberacion(version=version, novedades=novedades.strip()[:2000] or None))
        _bitacora(db, None, "versión liberada", f"v{version}")
        db.commit()
    return _ir("/versiones", f"v{version} liberada: cada ISP la verá en Configuración → Mi cuenta")


@app.get("/whatsapp", response_class=HTMLResponse)
def whatsapp_pagina(request: Request, msg: str = "", csrf: str = Depends(sesion)):
    estado = whatsapp.estado()
    qr = whatsapp.qr_svg(estado["qr"]) if estado["qr"] and not estado["conectado"] else None
    return _render(request, "whatsapp.html", csrf, estado=estado, qr=qr, msg=msg)


@app.post("/whatsapp/cerrar")
def whatsapp_cerrar(_csrf_ok: str = Depends(sesion)):
    try:
        whatsapp.cerrar_sesion()
    except whatsapp.ErrorWhatsApp as error:
        return _ir("/whatsapp", f"Error: {error}")
    with Sesion() as db:
        _bitacora(db, None, "whatsapp de ventas desvinculado")
        db.commit()
    return _ir("/whatsapp", "Número desvinculado; escanea el código con el nuevo")


@app.post("/whatsapp/webhook/{tipo}")
async def whatsapp_webhook(tipo: str, request: Request):
    """El bot avisa de mensajes y confirmaciones; el panel no los guarda (se ven en el celular)."""
    recibido = request.headers.get("X-Webhook-Secret", "")
    if not whatsapp.SECRETO or not hmac.compare_digest(recibido, whatsapp.SECRETO):
        raise HTTPException(status_code=403)
    return {"ok": True, "matched": True}


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
    hoy = date.today()
    with Sesion() as db:
        registro = _isp_firmado(db, isp, "cuenta", vence, firma)
        corte = _corte(registro)
        aviso = aviso_al_isp(registro.pagado_hasta, registro.suspendido, corte, hoy,
                             HORA_CORTE, DIAS_AVISO, CONTACTO_PAGO)
        liberada = _liberada(db)
    vigente_hasta = registro.demo_hasta if registro.es_demo else registro.pagado_hasta
    nueva = liberada and _orden_version(liberada.version) > _orden_version(registro.version)
    return {
        "aviso": aviso,
        "cuenta": {
            "plan": registro.plan,
            "es_demo": registro.es_demo,
            "precio_mensual": str(registro.precio_mensual or 0),
            "vigente_hasta": vigente_hasta.isoformat() if vigente_hasta else None,
            "dias_restantes": (vigente_hasta - hoy).days if vigente_hasta else None,
            "corte": corte.isoformat() if corte else None,
            "contacto_pago": CONTACTO_PAGO or None,
        },
        "version": {
            "instalada": registro.version,
            "disponible": liberada.version if nueva else None,
            "novedades": liberada.novedades if nueva else None,
            "fecha": liberada.fecha.date().isoformat() if nueva else None,
            "actualizando": registro.slug in _actualizando,
        },
    }


_actualizando: set[str] = set()


def _actualizar_isp(isp_id: int, version: str, quien: str) -> None:
    """Hilo: pasa al ISP a la versión dada sin tocar su bot ni sus límites."""
    with Sesion() as db:
        isp = db.get(Isp, isp_id)
        slug, antes = isp.slug, isp.version
        try:
            docker_ops.aplicar_ajustes(
                slug, version=version, whatsapp=isp.whatsapp, limite_clientes=isp.limite_clientes,
                limite_routers=isp.limite_routers, encendido=not isp.suspendido,
            )
        except (docker_ops.ErrorDocker, OSError) as error:
            log.error("No se pudo actualizar %s a %s: %s", slug, version, error)
            _bitacora(db, isp.id, "actualización falló", f"v{antes} → v{version} ({quien}): {str(error)[-300:]}")
        else:
            isp.version = version
            _bitacora(db, isp.id, "actualizado", f"v{antes} → v{version} ({quien})")
        finally:
            db.commit()
            _actualizando.discard(slug)


@app.post("/cuenta/actualizar")
def cuenta_actualizar(isp: str = "", vence: str = "", firma: str = ""):
    """El admin del ISP pidió pasarse a la versión liberada."""
    with Sesion() as db:
        registro = _isp_firmado(db, isp, "actualizar", vence, firma)
        liberada = _liberada(db)
        if registro.suspendido:
            raise HTTPException(status_code=409, detail="El sistema está suspendido")
        if not liberada or _orden_version(liberada.version) <= _orden_version(registro.version):
            raise HTTPException(status_code=409, detail="Ya tienes la versión más reciente")
        if registro.slug in _actualizando:
            return {"ok": True, "version": liberada.version}
        try:
            construidas = docker_ops.versiones_disponibles()
        except (docker_ops.ErrorDocker, OSError):
            construidas = []
        if liberada.version not in construidas:
            raise HTTPException(status_code=409, detail="La actualización no está disponible ahora; intenta más tarde")
        _actualizando.add(registro.slug)
        _bitacora(db, registro.id, "actualización pedida", f"v{registro.version} → v{liberada.version}")
        db.commit()
        isp_id, version = registro.id, liberada.version
    threading.Thread(target=_actualizar_isp, args=(isp_id, version, "lo pidió el ISP"), daemon=True).start()
    return {"ok": True, "version": version}


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
