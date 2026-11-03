"""Mensualidad de cada ISP: fechas, pagos y estado. Sin Docker ni base de datos."""
import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

SLUG = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
PRIMER_PUERTO_WEB = 18001
PRIMER_PUERTO_WG = 51821


def sumar_meses(fecha: date, meses: int, dia_pago: int | None = None) -> date:
    """Suma meses respetando el día de pago (31 → último día del mes corto)."""
    total = fecha.month - 1 + meses
    anio, mes = fecha.year + total // 12, total % 12 + 1
    dia = min(dia_pago or fecha.day, calendar.monthrange(anio, mes)[1])
    return date(anio, mes, dia)


def nuevo_vencimiento(pagado_hasta: date | None, meses: int, dia_pago: int, hoy: date) -> date:
    """Un pago se suma sobre lo ya pagado; si estaba vencido, cuenta desde hoy."""
    base = pagado_hasta if pagado_hasta and pagado_hasta >= hoy else hoy
    return sumar_meses(base, meses, dia_pago)


@dataclass(frozen=True)
class Estado:
    clave: str  # al_corriente, por_vencer, gracia, vencido, suspendido, sin_pago
    texto: str
    tono: str  # verde, ambar, rojo, gris


def estado_cuenta(
    pagado_hasta: date | None,
    suspendido: bool,
    hoy: date,
    dias_aviso: int = 5,
    dias_gracia: int = 5,
) -> Estado:
    if suspendido:
        return Estado("suspendido", "Suspendido", "gris")
    if pagado_hasta is None:
        return Estado("sin_pago", "Sin pagos registrados", "ambar")
    dias = (pagado_hasta - hoy).days
    if dias > dias_aviso:
        return Estado("al_corriente", f"Al corriente hasta {pagado_hasta:%d/%m/%Y}", "verde")
    if dias >= 0:
        cuando = "hoy" if dias == 0 else f"en {dias} día{'s' if dias != 1 else ''}"
        return Estado("por_vencer", f"Vence {cuando}", "ambar")
    atraso = -dias
    if atraso <= dias_gracia:
        return Estado("gracia", f"Vencido hace {atraso} día{'s' if atraso != 1 else ''} (en gracia)", "rojo")
    return Estado("vencido", f"Vencido hace {atraso} días", "rojo")


def dia_de_corte(pagado_hasta: date | None, dias_gracia: int, prorroga_hasta: date | None = None) -> date | None:
    """Primer día en que el ISP queda suspendido si no paga (después de la gracia y la prórroga)."""
    if pagado_hasta is None:
        return None  # sin pagos registrados (alta de prueba): no se corta solo
    corte = pagado_hasta + timedelta(days=dias_gracia + 1)
    if prorroga_hasta and prorroga_hasta >= corte:
        corte = prorroga_hasta + timedelta(days=1)
    return corte


def debe_suspenderse(
    pagado_hasta: date | None,
    suspendido: bool,
    corte_automatico: bool,
    prorroga_hasta: date | None,
    hoy: date,
    dias_gracia: int = 5,
) -> bool:
    if suspendido or not corte_automatico:
        return False
    corte = dia_de_corte(pagado_hasta, dias_gracia, prorroga_hasta)
    return corte is not None and hoy >= corte


MESES = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
         "septiembre", "octubre", "noviembre", "diciembre")


def _cuando(fecha: date, hoy: date) -> str:
    if fecha == hoy:
        return "hoy"
    if fecha == hoy + timedelta(days=1):
        return "mañana"
    return f"el {fecha.day} de {MESES[fecha.month - 1]}"


def aviso_al_isp(
    pagado_hasta: date | None,
    suspendido: bool,
    corte: date | None,
    hoy: date,
    hora_corte: str = "",
    dias_aviso: int = 5,
    contacto: str = "",
) -> dict | None:
    """Lo que ve el ISP en su propio admin antes de que se le corte; None si no hay nada que avisar.

    corte es el día en que el panel lo suspenderá solo (None si el corte está apagado).
    """
    if suspendido or pagado_hasta is None:
        return None
    dias = (pagado_hasta - hoy).days
    if dias > dias_aviso:
        return None
    if dias >= 0:
        tono = "ambar"
        mensaje = f"Tu mensualidad de WisPay vence {_cuando(pagado_hasta, hoy)}."
        if corte:
            mensaje += f" Si no se registra tu pago, el sistema se suspende {_cuando(corte, hoy)}."
    else:
        tono = "rojo"
        mensaje = f"Tu mensualidad de WisPay venció {_cuando(pagado_hasta, hoy)}."
        if corte:
            a_la_hora = f" a las {hora_corte}" if hora_corte and corte <= hoy else ""
            mensaje += f" El sistema se suspende {_cuando(max(corte, hoy), hoy)}{a_la_hora} si no se registra tu pago."
        else:
            mensaje += " Regulariza tu pago para evitar la suspensión."
    if contacto:
        mensaje += f" Para pagar: {contacto}."
    return {
        "tono": tono,
        "mensaje": mensaje,
        "pagado_hasta": pagado_hasta.isoformat(),
        "corte": corte.isoformat() if corte else None,
    }


def siguiente_puerto(usados: list[int], primero: int) -> int:
    libres = set(usados)
    puerto = primero
    while puerto in libres:
        puerto += 1
    return puerto


def poner_variable(contenido: str, clave: str, valor: str) -> str:
    """Cambia o agrega CLAVE=valor en un archivo .env sin tocar las demás líneas."""
    linea = f"{clave}={valor}"
    patron = re.compile(rf"^{re.escape(clave)}=.*$", re.MULTILINE)
    if patron.search(contenido):
        return patron.sub(lambda _: linea, contenido, count=1)
    if contenido and not contenido.endswith("\n"):
        contenido += "\n"
    return f"{contenido}{linea}\n"


def segundos_hasta(hora: str, ahora: datetime) -> float:
    """Segundos para la próxima vez que el reloj marque HH:MM (hoy o mañana)."""
    horas, minutos = (int(parte) for parte in hora.split(":"))
    objetivo = ahora.replace(hour=horas, minute=minutos, second=0, microsecond=0)
    if objetivo <= ahora:
        objetivo += timedelta(days=1)
    return (objetivo - ahora).total_seconds()


def estado_respaldo(
    fecha: datetime | None,
    error: str | None,
    remoto: str | None,
    creado: datetime,
    ahora: datetime,
    horas_maximas: int = 36,
) -> Estado:
    """Cómo va el último respaldo de un ISP; ámbar o rojo piden revisarlo."""
    if error:
        return Estado("fallo", f"Falló el {fecha:%d/%m %H:%M}: {error}", "rojo")
    if fecha is None:
        if ahora - creado > timedelta(hours=horas_maximas):
            return Estado("sin_respaldo", "Nunca se ha respaldado", "rojo")
        return Estado("pendiente", "Todavía sin respaldo", "gris")
    if ahora - fecha > timedelta(hours=horas_maximas):
        return Estado("atrasado", f"Último respaldo el {fecha:%d/%m/%Y}", "rojo")
    if remoto and remoto.startswith("error"):
        return Estado("solo_local", f"Solo en la VPS; la copia externa falló ({remoto[7:].strip()})", "ambar")
    donde = " y fuera de la VPS" if remoto == "ok" else " (solo en la VPS)"
    return Estado("ok", f"Respaldado el {fecha:%d/%m %H:%M}{donde}", "verde")
