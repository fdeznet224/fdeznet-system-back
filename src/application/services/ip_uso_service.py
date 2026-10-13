"""IP de una red que el MikroTik ya está usando aunque no haya cliente con ella.

El sistema solo conoce las IP de sus clientes; la casa del dueño, el site o
equipos de familiares pueden tener IP fija dentro de la misma red. Antes de
ofrecer una IP como libre se cruza con lo que ve el MikroTik: secrets PPPoE,
sesiones activas, ARP, leases DHCP y sus propias direcciones.
"""

import asyncio
import ipaddress
import logging
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from src.application.services.ipam_service import IPAMService
from src.infrastructure.mikrotik_service import MikroTikService
from src.infrastructure.models import RedModel, RouterModel

logger = logging.getLogger(__name__)


def _ip(valor: Any) -> Optional[str]:
    texto = str(valor or "").strip().split("/")[0]
    try:
        ip = ipaddress.ip_address(texto)
    except ValueError:
        return None
    return str(ip) if isinstance(ip, ipaddress.IPv4Address) else None


def ips_vistas(
    secrets: List[Dict[str, Any]],
    activos: List[Dict[str, Any]],
    arp: List[Dict[str, Any]],
    leases: List[Dict[str, Any]],
    propias: List[Dict[str, Any]],
    ignorar_usuario: Optional[str] = None,
) -> Dict[str, str]:
    """IP → de dónde la vio el MikroTik (el primer origen gana).

    `ignorar_usuario`: el PPPoE del propio servicio, que no choca consigo mismo.
    """
    vistas: Dict[str, str] = {}
    propio = (ignorar_usuario or "").strip()

    def anotar(ip: Optional[str], origen: str) -> None:
        if ip and ip not in vistas:
            vistas[ip] = origen

    for item in propias:
        anotar(_ip(item.get("address")), f"Dirección del MikroTik ({item.get('interface') or 'sin interfaz'})")
    for item in activos:
        if propio and item.get("name") == propio:
            continue
        anotar(_ip(item.get("address")), f"Sesión PPPoE activa: {item.get('name') or '?'}")
    for item in secrets:
        if propio and item.get("name") == propio:
            continue
        anotar(_ip(item.get("remote-address")), f"Usuario PPPoE: {item.get('name') or '?'}")
    for item in leases:
        nombre = item.get("host-name") or item.get("comment") or item.get("mac-address") or "?"
        anotar(_ip(item.get("address")), f"DHCP: {nombre}")
    for item in arp:
        if str(item.get("complete", "true")).lower() == "false" and not item.get("mac-address"):
            continue
        detalle = item.get("comment") or item.get("mac-address") or "?"
        anotar(_ip(item.get("address")), f"Equipo en ARP: {detalle}")
    return vistas


def _leer_mikrotik(router: RouterModel, ignorar_usuario: Optional[str] = None) -> Dict[str, str]:
    mk = MikroTikService(router.ip_vpn, router.user_api, router.pass_api, router.port_api)

    def lista(ruta: str, obligatoria: bool) -> List[Dict[str, Any]]:
        try:
            datos = mk._request("GET", ruta, raise_on_error=obligatoria)
        except Exception:
            if obligatoria:
                raise
            return []
        return datos if isinstance(datos, list) else []

    return ips_vistas(
        secrets=lista("/ppp/secret", True),
        activos=lista("/ppp/active", True),
        arp=lista("/ip/arp", False),
        leases=lista("/ip/dhcp-server/lease", False),
        propias=lista("/ip/address", False),
        ignorar_usuario=ignorar_usuario,
    )


class IpUsoService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def ips_en_mikrotik(
        self, red: RedModel, ignorar_usuario: Optional[str] = None,
    ) -> Tuple[Dict[str, str], Optional[str]]:
        """IP de la red que usa el MikroTik, o el error si no se pudo leer."""
        router = await self.db.get(RouterModel, red.router_id) if red.router_id else None
        if not router:
            return {}, "La red no tiene MikroTik asignado"
        try:
            vistas = await asyncio.to_thread(_leer_mikrotik, router, ignorar_usuario)
        except Exception as exc:
            logger.warning("No se leyeron las IP del MikroTik %s: %s", router.nombre, exc)
            return {}, f"No se pudo consultar el MikroTik {router.nombre}"
        red_ip = IPAMService._red_ipv4(red)
        return {ip: origen for ip, origen in vistas.items() if ipaddress.ip_address(ip) in red_ip}, None

    async def uso_de_red(
        self,
        red: RedModel,
        excluir_servicio_id: Optional[int] = None,
        excluir_cliente_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Libres de verdad y las que el MikroTik usa sin cliente en el sistema."""
        en_mikrotik, error = await self.ips_en_mikrotik(red)
        ipam = IPAMService(self.db)
        del_sistema = await ipam._ips_ocupadas()
        disponibles = await ipam.listar_disponibles(
            red, limite=254, excluir_cliente_id=excluir_cliente_id,
            excluir_servicio_id=excluir_servicio_id, evitar=set(en_mikrotik),
        )
        fuera = [
            {"ip": ip, "origen": origen}
            for ip, origen in sorted(en_mikrotik.items(), key=lambda par: ipaddress.ip_address(par[0]))
            if ip not in del_sistema and ip != _ip(red.gateway)
        ]
        return {
            "red": red.nombre,
            "cidr": red.cidr,
            "disponibles": disponibles,
            "fuera_del_sistema": fuera,
            "mikrotik_verificado": error is None,
            "error": error,
        }
