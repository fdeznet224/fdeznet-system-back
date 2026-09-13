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
