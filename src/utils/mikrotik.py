import re


MAC_PATTERN = re.compile(r"^[0-9A-F]{2}(?::[0-9A-F]{2}){5}$")


def normalizar_mac(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    compact = re.sub(r"[^0-9A-Fa-f]", "", value)
    if len(compact) != 12:
        raise ValueError("La MAC debe contener 12 dígitos hexadecimales")
    normalized = ":".join(
        compact[index:index + 2].upper()
        for index in range(0, 12, 2)
    )
    if not MAC_PATTERN.fullmatch(normalized):
        raise ValueError("La dirección MAC no es válida")
    return normalized


def _velocidad(value: int | None) -> str:
    amount = int(value or 0)
    if amount <= 0:
        return "0"
    if amount >= 1024:
        megas = amount / 1024
        return f"{int(megas)}M" if megas.is_integer() else f"{megas:.1f}M"
    return f"{amount}k"


def formatear_rate_limit_pppoe(plan) -> str:
    maximum = (
        f"{_velocidad(plan.velocidad_subida)}/"
        f"{_velocidad(plan.velocidad_bajada)}"
    )
    if (plan.burst_subida or 0) > 0 or (plan.burst_bajada or 0) > 0:
        burst = (
            f"{_velocidad(plan.burst_subida)}/"
            f"{_velocidad(plan.burst_bajada)}"
        )
        threshold = maximum
        burst_time = f"{plan.burst_time}/{plan.burst_time}"
    else:
        burst = threshold = burst_time = "0/0"
    guarantee_up = int(
        plan.velocidad_subida * (plan.garantia_percent / 100)
    )
    guarantee_down = int(
        plan.velocidad_bajada * (plan.garantia_percent / 100)
    )
    minimum = f"{_velocidad(guarantee_up)}/{_velocidad(guarantee_down)}"
    return (
        f"{maximum} {burst} {threshold} {burst_time} "
        f"{plan.prioridad} {minimum}"
    )


def formatear_rate_limit_dhcp(plan) -> str:
    maximum = (
        f"{_velocidad(plan.velocidad_subida)}/"
        f"{_velocidad(plan.velocidad_bajada)}"
    )
    if (plan.burst_subida or 0) > 0 or (plan.burst_bajada or 0) > 0:
        burst = (
            f"{_velocidad(plan.burst_subida)}/"
            f"{_velocidad(plan.burst_bajada)}"
        )
        return (
            f"{maximum} {burst} {maximum} "
            f"{plan.burst_time}/{plan.burst_time}"
        )
    return maximum


# Prefijos IPv4 de Meta (AS32934). WhatsApp comparte infraestructura con
# Facebook e Instagram, por eso el modo suspendido también limita velocidad:
# esas apps quedan prácticamente inutilizables y el chat sigue funcionando.
RANGOS_META_WHATSAPP = (
    "31.13.24.0/21",
    "31.13.64.0/18",
    "45.64.40.0/22",
    "57.144.0.0/14",
    "66.220.144.0/20",
    "69.63.176.0/20",
    "69.171.224.0/19",
    "74.119.76.0/22",
    "102.132.96.0/20",
    "103.4.96.0/22",
    "129.134.0.0/16",
    "147.75.208.0/20",
    "157.240.0.0/16",
    "163.70.128.0/17",
    "163.77.128.0/17",
    "173.252.64.0/18",
    "179.60.192.0/22",
    "185.60.216.0/22",
    "185.89.216.0/22",
    "204.15.20.0/22",
)

KBPS_CORTE_WHATSAPP_DEFAULT = 128


def debe_bloquear_acceso(suspendido: bool, solo_whatsapp: bool) -> bool:
    """Indica si el secret PPPoE o el lease DHCP debe deshabilitarse.

    En modo "solo WhatsApp" el suspendido conserva su sesión y el corte se
    aplica únicamente con CORTE_FDEZNET, para que el firewall deje pasar
    WhatsApp a baja velocidad.
    """
    return suspendido and not solo_whatsapp
