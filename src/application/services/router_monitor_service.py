"""Reglas del monitoreo de routers para no alertar por fallas momentáneas."""

FALLAS_PARA_OFFLINE = 3
HANDSHAKE_VIGENTE_SEGUNDOS = 180


def evaluar_estado_router(
    estaba_online: bool, respondio: bool, fallas_previas: int
) -> tuple[bool, int]:
    """Devuelve (nuevo estado online, fallas consecutivas acumuladas).

    Una respuesta basta para marcarlo en línea; para marcarlo caído se exigen
    varias fallas seguidas, así un enlace inestable no genera alertas
    ONLINE/OFFLINE cada minuto.
    """
    if respondio:
        return True, 0
    fallas = fallas_previas + 1
    if estaba_online and fallas < FALLAS_PARA_OFFLINE:
        return True, fallas
    return False, fallas


def describir_enlace_vpn(ultimo_handshake: int | None, ahora: float) -> str:
    """Explica si la caída es del túnel WireGuard o sólo de la API del router."""
    if ultimo_handshake is None:
        return ""
    if ultimo_handshake == 0:
        return "El túnel VPN nunca se ha conectado."
    minutos = int(ahora - ultimo_handshake) // 60
    if ahora - ultimo_handshake > HANDSHAKE_VIGENTE_SEGUNDOS:
        return f"Túnel VPN caído (último contacto hace {minutos} min)."
    return "El túnel VPN sigue activo, pero el MikroTik no responde a la API (enlace lento o saturado)."
