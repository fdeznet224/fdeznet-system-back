"""Cómo se conectan los clientes de un MikroTik: PPPoE, DHCP o IP fija.

El modo lo define el router (Gestión de Tráfico) y vale para todos sus
clientes; el formulario de alta pide solo lo de ese modo.

- pppoe: usuario y contraseña (secret con el perfil del plan).
- dhcp: lease fijo IP + MAC con la velocidad del plan.
- estatica: el cliente trae la IP puesta; solo una cola simple con la
  velocidad del plan. Nada de usuario ni MAC obligatoria.

En los tres el corte es el mismo: la IP en la lista CORTE_FDEZNET.
"""
from typing import Any, Optional

MODOS_ACCESO = ("pppoe", "dhcp", "estatica")
NOMBRES = {"pppoe": "PPPoE", "dhcp": "DHCP", "estatica": "IP fija"}


def _valor(modo: Any) -> str:
    return str(getattr(modo, "value", modo) or "").strip().lower()


def modo_router(router: Any) -> str:
    modo = _valor(getattr(router, "tipo_seguridad", None)) if router is not None else ""
    return modo if modo in MODOS_ACCESO else "pppoe"


def modo_acceso(objeto: Any = None, router: Any = None) -> str:
    """Modo con que el servicio (o cliente) entra al MikroTik: el de su router."""
    if router is None and objeto is not None:
        router = getattr(objeto, "router", None)
    return modo_router(router)


# --- Operaciones en el MikroTik según el modo --------------------------------
# Funciones síncronas (se corren con asyncio.to_thread). "datos" es el servicio
# o el cliente: ip_asignada, mac_address, user_pppoe, pass_pppoe.

def identificador(modo: str, datos: Any) -> Optional[str]:
    """Lo que identifica al cliente en el MikroTik según el modo."""
    from src.utils.mikrotik import normalizar_mac

    if modo == "dhcp":
        return normalizar_mac(getattr(datos, "mac_address", None))
    if modo == "estatica":
        return (getattr(datos, "ip_asignada", None) or "").strip() or None
    return (getattr(datos, "user_pppoe", None) or "").strip() or None


def faltantes(modo: str, datos: Any) -> list[str]:
    """Qué le falta al cliente para entrar al MikroTik en su modo."""
    falta = []
    if modo == "dhcp" and not getattr(datos, "mac_address", None):
        falta.append("MAC WAN/CPE")
    if modo == "pppoe":
        if not getattr(datos, "user_pppoe", None):
            falta.append("usuario PPPoE")
        if not getattr(datos, "pass_pppoe", None):
            falta.append("contraseña PPPoE")
    if not getattr(datos, "ip_asignada", None):
        falta.append("IP")
    return falta


def aplicar(mk, modo: str, datos: Any, plan: Any, comentario: str):
    """Crea o corrige el acceso con la velocidad del plan."""
    from src.utils.mikrotik import formatear_cola_estatica, formatear_rate_limit_dhcp

    if modo == "dhcp":
        return mk.crear_actualizar_lease_dhcp(datos.mac_address, datos.ip_asignada,
                                              formatear_rate_limit_dhcp(plan), comentario)
    if modo == "estatica":
        return mk.crear_actualizar_cola_estatica(datos.ip_asignada, formatear_cola_estatica(plan), comentario)
    return mk.crear_actualizar_pppoe(datos.user_pppoe, datos.pass_pppoe, plan.nombre, datos.ip_asignada, comentario)


def bloquear(mk, modo: str, datos: Any, bloqueado: bool):
    """Apaga/enciende el acceso (PPPoE deshabilitado, lease bloqueado).

    La IP fija no tiene acceso que apagar: solo confirma que existe; su corte
    es la lista CORTE_FDEZNET, igual que los otros.
    """
    if modo == "dhcp":
        return mk.activar_desactivar_dhcp(datos.mac_address, bloqueado)
    if modo == "estatica":
        return mk.activar_desactivar_estatica(datos.ip_asignada, bloqueado)
    return mk.activar_desactivar_pppoe(datos.user_pppoe, bloqueado)


def obtener(mk, modo: str, datos: Any):
    if modo == "dhcp":
        return mk.obtener_lease_dhcp_estricto(datos.mac_address)
    if modo == "estatica":
        return mk.obtener_cola_estatica_estricto(datos.ip_asignada)
    return mk.obtener_pppoe_estricto(datos.user_pppoe)


def eliminar(mk, modo: str, datos: Any):
    if modo == "dhcp":
        return mk.eliminar_lease_dhcp(datos.mac_address)
    if modo == "estatica":
        return mk.eliminar_cola_estatica(datos.ip_asignada)
    return mk.eliminar_pppoe_user(datos.user_pppoe)


# --- DHCP: la IP que el cliente ya tomó --------------------------------------

async def ip_del_lease(db, router: Any, mac: Optional[str]) -> dict:
    """La IP que el MikroTik le dio por DHCP a esa MAC (lease dinámico o fijo).

    Al activar, el lease se vuelve estático con esa IP y la velocidad del plan,
    así el cliente no cambia de IP. Solo se ofrece si es de una red del router
    y ningún otro servicio la tiene.
    """
    import asyncio
    import ipaddress

    from sqlalchemy import or_, select

    from src.infrastructure.mikrotik_service import MikroTikService
    from src.infrastructure.models import RedModel, ServicioModel
    from src.utils.mikrotik import normalizar_mac

    mac = normalizar_mac(mac)
    if not router or not mac:
        return {"encontrado": False}
    mk = MikroTikService(router.ip_vpn, router.user_api, router.pass_api, router.port_api)
    lease = await asyncio.to_thread(mk.obtener_lease_dhcp_estricto, mac)
    ip = str((lease or {}).get("address") or "").strip()
    if not ip:
        return {"encontrado": False}
    redes = (await db.execute(select(RedModel).where(RedModel.router_id == router.id))).scalars().all()
    red = None
    for candidata in redes:
        try:
            if ipaddress.ip_address(ip) in ipaddress.ip_network(candidata.cidr, strict=False):
                red = candidata
                break
        except ValueError:
            continue
    ocupante = (
        await db.execute(
            select(ServicioModel.id).where(
                ServicioModel.ip_asignada == ip,
                ServicioModel.estado != "cancelado",
                or_(ServicioModel.mac_address.is_(None), ServicioModel.mac_address != mac),
            ).limit(1)
        )
    ).first()
    return {
        "encontrado": True,
        "ip": ip,
        "dinamico": str(lease.get("dynamic", "false")).lower() in {"true", "yes", "1"},
        "equipo": lease.get("host-name") or None,
        "red_id": red.id if red else None,
        "usable": bool(red) and not ocupante,
    }
