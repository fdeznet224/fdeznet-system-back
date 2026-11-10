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


# --- Línea de internet (WAN) principal o de respaldo ------------------------
#
# Se leen las rutas por defecto (0.0.0.0/0) de la tabla main del MikroTik. La
# principal es la de menor distancia; si la que está activa tiene una distancia
# mayor, el router está saliendo por un respaldo. Si un netwatch deshabilita la
# ruta principal al caerse, se reconoce porque se recuerda cuál era.

WAN_PRINCIPAL = "principal"
WAN_RESPALDO = "respaldo"
WAN_SIN_INTERNET = "sin_internet"
LECTURAS_PARA_CAMBIO_WAN = 2


def _verdadero(valor) -> bool:
    return str(valor).strip().lower() in {"true", "yes", "1"}


def _distancia(ruta: dict) -> int:
    try:
        return int(ruta.get("distance") or 1)
    except (TypeError, ValueError):
        return 1


def clave_ruta(ruta: dict) -> str:
    """Identifica la línea por su puerta de enlace (no cambia entre lecturas)."""
    return str(ruta.get("gateway") or ruta.get("immediate-gw") or "").strip()


def nombre_enlace(ruta: dict) -> str:
    """Texto para el aviso: el comentario de la ruta o su puerta e interfaz."""
    comentario = str(ruta.get("comment") or "").strip()
    puerta = clave_ruta(ruta)
    interfaz = ""
    inmediata = str(ruta.get("immediate-gw") or "")
    if "%" in inmediata:
        interfaz = inmediata.split("%", 1)[1]
    detalle = puerta if not interfaz or interfaz == puerta else f"{puerta} por {interfaz}"
    if comentario:
        return f"{comentario} ({detalle})" if detalle else comentario
    return detalle or "ruta sin nombre"


def _rutas_main(rutas: list[dict]) -> list[dict]:
    return [
        r for r in rutas
        if str(r.get("dst-address") or "0.0.0.0/0") == "0.0.0.0/0"
        and str(r.get("routing-table") or "main") == "main"
        and not r.get("routing-mark")
        and clave_ruta(r)
        and str(r.get("type") or "").lower() not in {"blackhole", "unreachable", "prohibit"}
        and not _verdadero(r.get("blackhole"))
    ]


def evaluar_wan(rutas: list[dict] | None, principal_guardada: str | None) -> dict | None:
    """Devuelve {estado, enlace, clave_activa, principal} o None si no hay rutas.

    Sin rutas por defecto (router sin WAN configurada) no se opina.
    """
    if rutas is None:
        return None
    candidatas = _rutas_main(rutas)
    if not candidatas:
        return None
    habilitadas = [r for r in candidatas if not _verdadero(r.get("disabled"))]
    # La principal recordada cuenta aunque un script la haya deshabilitado.
    para_principal = habilitadas + [
        r for r in candidatas
        if _verdadero(r.get("disabled")) and principal_guardada and clave_ruta(r) == principal_guardada
    ]
    if not para_principal:
        return None
    menor = min(_distancia(r) for r in para_principal)
    activas = sorted(
        (r for r in habilitadas if _verdadero(r.get("active"))),
        key=_distancia,
    )
    if not activas:
        principal = min(para_principal, key=_distancia)
        return {
            "estado": WAN_SIN_INTERNET,
            "enlace": None,
            "clave_activa": None,
            "principal": principal_guardada or clave_ruta(principal),
            "nombre_principal": nombre_enlace(principal),
        }
    activa = activas[0]
    principal = min(para_principal, key=_distancia)
    if _distancia(activa) <= menor:
        return {
            "estado": WAN_PRINCIPAL,
            "enlace": nombre_enlace(activa),
            "clave_activa": clave_ruta(activa),
            "principal": clave_ruta(activa),
            "nombre_principal": nombre_enlace(activa),
        }
    return {
        "estado": WAN_RESPALDO,
        "enlace": nombre_enlace(activa),
        "clave_activa": clave_ruta(activa),
        "principal": principal_guardada or clave_ruta(principal),
        "nombre_principal": nombre_enlace(principal),
    }


def mensaje_cambio_wan(router: str, anterior: str | None, nuevo: dict, fecha: str) -> str | None:
    """Aviso para WhatsApp cuando cambia la línea; None si no hay que avisar."""
    estado = nuevo["estado"]
    if estado == anterior:
        return None
    if estado == WAN_RESPALDO:
        return (
            f"⚠️ AVISO Internet: {router} perdió la línea principal "
            f"({nuevo['nombre_principal']}) y está saliendo por el respaldo "
            f"{nuevo['enlace']} desde el {fecha}."
        )
    if estado == WAN_SIN_INTERNET:
        return (
            f"❌ AVISO Internet: {router} no tiene ninguna línea de internet activa "
            f"desde el {fecha}. Los clientes de ese router están sin servicio."
        )
    if anterior in (WAN_RESPALDO, WAN_SIN_INTERNET):
        return (
            f"✅ AVISO Internet: {router} volvió a la línea principal "
            f"{nuevo['enlace']} el {fecha}."
        )
    return None  # Primera lectura o ya estaba en la principal.
