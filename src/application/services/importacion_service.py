"""Servicio de los clientes que llegan por el Excel de importación.

Son clientes que ya están instalados y navegando en el MikroTik: su servicio
nace activo, sin orden de instalación, sin tocar el router y sin prorrateo. La
primera mensualidad completa sale el próximo día de pago de su plantilla.
"""
import calendar
import ipaddress
from datetime import date
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.services.billing_calendar_service import BillingCalendarService
from src.infrastructure.models import (
    CicloFacturacion,
    ClienteModel,
    PlantillaFacturacionModel,
    ServicioModel,
    TipoFacturacion,
)
from src.utils.mikrotik import formatear_cola_estatica, formatear_rate_limit_dhcp, mismos_limites, normalizar_mac


def proximo_dia_de_pago(hoy: date, dia_pago: int | None) -> date:
    """Hoy si hoy es el día de pago; si no, el siguiente (31 → último día del mes)."""
    if not dia_pago:
        return hoy
    dia = min(dia_pago, calendar.monthrange(hoy.year, hoy.month)[1])
    if dia >= hoy.day:
        return hoy.replace(day=dia)
    siguiente = BillingCalendarService.add_months(hoy.replace(day=1), 1)
    return siguiente.replace(day=min(dia_pago, calendar.monthrange(siguiente.year, siguiente.month)[1]))


async def crear_servicio_importado(
    db: AsyncSession, cliente: ClienteModel, hoy: date | None = None
) -> ServicioModel:
    hoy = hoy or date.today()
    plantilla = (
        await db.get(PlantillaFacturacionModel, cliente.plantilla_id)
        if cliente.plantilla_id
        else None
    )
    ciclo = BillingCalendarService.resolver_ciclo(None, plantilla)
    dia_pago = plantilla.dia_pago if plantilla and ciclo == "calendario" else None
    primer_cobro = proximo_dia_de_pago(hoy, dia_pago)
    servicio = ServicioModel(
        cliente_id=cliente.id,
        alias="Principal",
        direccion=cliente.direccion,
        latitud=cliente.latitud,
        longitud=cliente.longitud,
        router_id=cliente.router_id,
        plan_id=cliente.plan_id,
        plantilla_id=cliente.plantilla_id,
        zona_id=cliente.zona_id,
        red_id=cliente.red_id,
        olt_id=cliente.olt_id,
        caja_nap_id=cliente.caja_nap_id,
        puerto_nap=cliente.puerto_nap,
        onu_id=cliente.onu_id,
        ip_asignada=cliente.ip_asignada,
        mac_address=cliente.mac_address,
        user_pppoe=cliente.user_pppoe,
        pass_pppoe=cliente.pass_pppoe,
        is_online=False,
        ultimo_cambio_estado=None,
        tipo_facturacion=TipoFacturacion.prepago,
        ciclo_facturacion=CicloFacturacion(ciclo),
        meses_gratis=0,
        fecha_instalacion=hoy,
        fecha_activacion=hoy,
        fecha_inicio_servicio=hoy,
        fecha_inicio_cobro=primer_cobro,
        proxima_facturacion=primer_cobro,
        dia_vencimiento=dia_pago if dia_pago else hoy.day,
        dias_tolerancia=(plantilla.dias_tolerancia or 0) if plantilla else 0,
        estado="activo",
    )
    db.add(servicio)
    await db.flush()
    return servicio


async def reparar_importados_sin_servicio(db: AsyncSession) -> int:
    """Crea el servicio de los clientes activos importados que quedaron sin él."""
    con_servicio = select(ServicioModel.cliente_id)
    clientes = (
        await db.execute(
            select(ClienteModel).where(
                ClienteModel.estado == "activo",
                ClienteModel.id.not_in(con_servicio),
            )
        )
    ).scalars().all()
    for cliente in clientes:
        await crear_servicio_importado(db, cliente)
    await db.commit()
    return len(clientes)


# --- Plantilla de importación según el modo del router ----------------------
# El Excel trae solo las columnas del modo: PPPoE (usuario y contraseña),
# DHCP (lease: IP + MAC) o IP fija (cola simple por IP). Nada de PPPoE ni ONU
# en un router que no las usa.

COLUMNAS_COMUNES = ["nombre_cliente", "numero_contrato", "ip_asignada", "telefono", "direccion",
                    "id_red", "id_zona", "id_plantilla_facturacion", "id_plan_sugerido"]
COLUMNAS_POR_MODO = {
    "pppoe": ["identificador_onu", "usuario_pppoe", "password_pppoe", "id_olt", "perfil_mikrotik_original"],
    "dhcp": ["tipo_conexion", "mac_antena", "ip_antena", "identificador_onu", "id_olt", "limite_mikrotik_original"],
    "estatica": ["tipo_conexion", "mac_antena", "ip_antena", "identificador_onu", "id_olt", "limite_mikrotik_original"],
}


def columnas_plantilla(modo: str) -> list[str]:
    return COLUMNAS_COMUNES + COLUMNAS_POR_MODO.get(modo, COLUMNAS_POR_MODO["pppoe"])


def _mac(valor) -> str | None:
    try:
        return normalizar_mac(str(valor)) if valor else None
    except ValueError:
        return None


def _en_red(ip: str, cidr: str | None) -> bool:
    if not cidr:
        return True
    try:
        return ipaddress.ip_address(ip) in ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return False


def _plan_por_limite(limite: str, planes: Iterable[Any], formato) -> int | None:
    """Plan cuya velocidad (subida/bajada) es la que tiene el MikroTik."""
    actual = str(limite or "").split(" ")[0]
    if not actual:
        return None
    for plan in planes:
        try:
            esperado = formato(plan)
        except (TypeError, AttributeError):
            continue
        if mismos_limites(actual, str(esperado).split(" ")[0]):
            return plan.id
    return None


def _plan_por_nombre(texto: str, mapa_planes: dict[str, int]) -> int | None:
    limpio = (texto or "").strip().lower()
    if not limpio:
        return None
    if limpio in mapa_planes:
        return mapa_planes[limpio]
    return next((pid for nombre, pid in mapa_planes.items() if nombre in limpio), None)


def _fila(base: dict, modo: str, **datos) -> dict:
    fila = {columna: "" for columna in columnas_plantilla(modo)}
    fila.update(base)
    fila.update(datos)
    return fila


def filas_pppoe(secrets: list[dict], base: dict, planes: list[Any], plan_defecto: int) -> list[dict]:
    mapa = {p.nombre.strip().lower(): p.id for p in planes if p.nombre}
    filas = []
    for item in secrets:
        perfil = item.get("profile", "") or item.get("comment", "") or ""
        ip = item.get("remote-address") or ""
        filas.append(_fila(
            base, "pppoe",
            nombre_cliente=item.get("comment") or item.get("name") or f"Cliente PPPoE {ip}",
            ip_asignada=ip,
            identificador_onu=item.get("caller-id") or "",
            usuario_pppoe=item.get("name", ""),
            password_pppoe=item.get("password", ""),
            id_plan_sugerido=_plan_por_nombre(perfil, mapa) or plan_defecto,
            perfil_mikrotik_original=perfil,
        ))
    return filas


def _tipo_por_defecto(base: dict) -> str:
    return "fibra" if int(base.get("id_olt") or 0) > 0 else "antena"


def filas_dhcp(leases: list[dict], base: dict, planes: list[Any], plan_defecto: int,
               cidr: str | None) -> list[dict]:
    """Leases de la red (fijos o dinámicos): IP, MAC del equipo y velocidad."""
    filas, vistas = [], set()
    for lease in leases:
        ip = str(lease.get("address") or "").strip()
        mac = _mac(lease.get("mac-address"))
        if not ip or not mac or mac in vistas or not _en_red(ip, cidr):
            continue
        vistas.add(mac)
        limite = str(lease.get("rate-limit") or "")
        filas.append(_fila(
            base, "dhcp",
            nombre_cliente=lease.get("comment") or lease.get("host-name") or f"Cliente DHCP {ip}",
            ip_asignada=ip,
            tipo_conexion=_tipo_por_defecto(base),
            mac_antena=mac,
            id_plan_sugerido=_plan_por_limite(limite, planes, formatear_rate_limit_dhcp) or plan_defecto,
            limite_mikrotik_original=limite,
        ))
    return filas


def filas_estatica(colas: list[dict], base: dict, planes: list[Any], plan_defecto: int,
                   cidr: str | None, arp: list[dict] | None = None) -> list[dict]:
    """Colas simples de una sola IP de la red; la MAC sale del ARP si se ve."""
    macs = {str(a.get("address")): _mac(a.get("mac-address")) for a in (arp or []) if a.get("address")}
    filas, vistas = [], set()
    for cola in colas:
        objetivos = [t.strip() for t in str(cola.get("target", "")).split(",") if t.strip()]
        if len(objetivos) != 1:
            continue
        ip, _, prefijo = objetivos[0].partition("/")
        if prefijo not in ("", "32"):
            continue
        if ip in vistas or not _en_red(ip, cidr):
            continue
        vistas.add(ip)
        limite = str(cola.get("max-limit") or "")
        filas.append(_fila(
            base, "estatica",
            nombre_cliente=cola.get("comment") or cola.get("name") or f"Cliente IP fija {ip}",
            ip_asignada=ip,
            tipo_conexion=_tipo_por_defecto(base),
            mac_antena=macs.get(ip) or "",
            id_plan_sugerido=_plan_por_limite(limite, planes, lambda p: formatear_cola_estatica(p)["max-limit"])
            or plan_defecto,
            limite_mikrotik_original=limite,
        ))
    return filas


def acceso_de_fila(modo: str, fila: dict) -> tuple[dict | None, str | None]:
    """Datos de acceso de una fila del Excel según el modo, o el motivo del error.

    Devuelve user_pppoe/pass_pppoe (solo PPPoE), mac_address, tipo_conexion e
    ip_antena. En DHCP la MAC es obligatoria; en IP fija, la IP.
    """
    def texto(*claves):
        return next((str(fila.get(c) or "").strip() for c in claves if str(fila.get(c) or "").strip()), "")

    tipo = texto("tipo_conexion").lower()
    radio = modo != "pppoe" and tipo in ("antena", "radio", "radioenlace")
    mac_texto = texto("mac_antena", "mac_address", "mac")
    mac = _mac(mac_texto)
    if mac_texto and not mac:
        return None, f"MAC {mac_texto} inválida"
    datos = {
        "user_pppoe": None, "pass_pppoe": None, "mac_address": mac,
        "tipo_conexion": "radio" if radio else "fibra",
        "ip_antena": (texto("ip_antena") or None) if radio else None,
    }
    if modo == "pppoe":
        datos["user_pppoe"] = texto("usuario_pppoe") or None
        datos["pass_pppoe"] = texto("password_pppoe") or None
    elif modo == "dhcp" and not mac:
        return None, "falta la MAC del equipo (mac_antena)"
    if modo == "estatica" and not texto("ip_asignada"):
        return None, "falta la IP (ip_asignada)"
    if datos["ip_antena"]:
        try:
            ipaddress.ip_address(datos["ip_antena"])
        except ValueError:
            return None, f"IP de antena {datos['ip_antena']} inválida"
    return datos, None
