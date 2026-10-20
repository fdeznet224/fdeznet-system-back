"""Consumo de datos por día de cada servicio.

Cada 5 minutos se leen de cada MikroTik los contadores de las interfaces
pppoe-in (una por sesión activa) y lo que crecieron desde la lectura anterior
se suma al día en consumo_diario. Cuando un cliente dice que no tuvo servicio,
la ficha muestra día por día cuántos datos usó y cuánto tiempo estuvo en línea.

Del lado del MikroTik, tx-byte es lo que bajó el cliente y rx-byte lo que subió.
"""

import asyncio
import logging
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.mikrotik_service import MikroTikService
from src.infrastructure.models import ConsumoDiarioModel, RouterModel, ServicioModel

logger = logging.getLogger(__name__)

# Más que esto entre dos lecturas no se cuenta como tiempo en línea: el
# servidor o el MikroTik no contestaron y no se sabe qué pasó en medio.
MAX_MINUTOS_ENTRE_LECTURAS = 15
# Se guarda poco más de un año para comparar contra el mismo mes.
DIAS_CONSUMO = 400
CAMPOS_INTERFAZ = "name,rx-byte,tx-byte,last-link-up-time"


@dataclass
class Muestra:
    sesion: str
    rx: int
    tx: int
    conectado_desde: datetime | None


@dataclass
class Estado:
    sesion: str | None
    rx: int | None
    tx: int | None
    lectura: datetime | None


def usuario_de_interfaz(nombre: str) -> str | None:
    """"<pppoe-Juan Perez>" → "Juan Perez"."""
    if nombre.startswith("<pppoe-") and nombre.endswith(">"):
        return nombre[len("<pppoe-"):-1]
    return None


def leer_fecha_mikrotik(valor) -> datetime | None:
    """RouterOS 7 da "2026-10-11 22:33:38"; el 6, "oct/11/2026 22:33:38"."""
    texto = str(valor or "").strip()
    for formato in ("%Y-%m-%d %H:%M:%S", "%b/%d/%Y %H:%M:%S"):
        try:
            return datetime.strptime(texto.capitalize() if "/" in texto else texto, formato)
        except ValueError:
            continue
    return None


def a_entero(valor) -> int:
    try:
        return max(int(valor), 0)
    except (TypeError, ValueError):
        return 0


def muestras_por_usuario(interfaces) -> dict[str, Muestra]:
    muestras = {}
    for interfaz in interfaces or []:
        usuario = usuario_de_interfaz(str(interfaz.get("name") or ""))
        if not usuario:
            continue
        muestras[usuario] = Muestra(
            sesion=str(interfaz.get(".id") or ""),
            rx=a_entero(interfaz.get("rx-byte")),
            tx=a_entero(interfaz.get("tx-byte")),
            conectado_desde=leer_fecha_mikrotik(interfaz.get("last-link-up-time")),
        )
    return muestras


def calcular_avance(estado: Estado | None, muestra: Muestra, ahora: datetime) -> tuple[int, int, int]:
    """(bajada, subida, minutos) que se suman al día con esta muestra.

    En la misma sesión cuenta lo que creció el contador. Si la sesión es nueva
    (el cliente se reconectó) sus contadores empezaron en cero, así que cuenta
    todo, siempre que haya empezado después de la lectura anterior o, sin
    lectura anterior, hoy; si no, solo se toma como punto de partida.
    """
    misma_sesion = bool(estado and estado.sesion and estado.sesion == muestra.sesion)
    if misma_sesion:
        bajada = muestra.tx - (estado.tx or 0)
        subida = muestra.rx - (estado.rx or 0)
        if bajada < 0 or subida < 0:  # el MikroTik reinició los contadores
            bajada, subida = muestra.tx, muestra.rx
        desde = estado.lectura
    else:
        inicio = muestra.conectado_desde
        anterior = estado.lectura if estado else None
        nueva = bool(inicio) and (inicio >= anterior if anterior else inicio.date() == ahora.date())
        bajada, subida = (muestra.tx, muestra.rx) if nueva else (0, 0)
        desde = max(inicio, datetime.combine(ahora.date(), datetime.min.time())) if nueva else None
    minutos = 0
    if desde:
        minutos = int((ahora - desde).total_seconds() // 60)
        minutos = min(max(minutos, 0), MAX_MINUTOS_ENTRE_LECTURAS)
    return bajada, subida, minutos


def resumen_mes(filas, anio: int, mes: int) -> dict:
    """Los días del mes con lo que usó cada uno (todos sus servicios juntos)."""
    dias_mes = monthrange(anio, mes)[1]
    por_dia = {
        date(anio, mes, d): {"fecha": date(anio, mes, d).isoformat(), "bajada": 0, "subida": 0, "minutos": 0}
        for d in range(1, dias_mes + 1)
    }
    for fila in filas:
        dia = por_dia.get(fila.fecha)
        if dia is None:
            continue
        dia["bajada"] += int(fila.bajada_bytes or 0)
        dia["subida"] += int(fila.subida_bytes or 0)
        dia["minutos"] = min(dia["minutos"] + int(fila.minutos_en_linea or 0), 24 * 60)
    dias = list(por_dia.values())
    return {
        "mes": f"{anio:04d}-{mes:02d}",
        "dias": dias,
        "total_bajada": sum(d["bajada"] for d in dias),
        "total_subida": sum(d["subida"] for d in dias),
        "dias_con_uso": sum(1 for d in dias if d["bajada"] + d["subida"] > 0),
    }


class ConsumoService:
    def __init__(self, db: AsyncSession, lector=None):
        self.db = db
        self.lector = lector or self._leer_interfaces

    @staticmethod
    async def _leer_interfaces(router: RouterModel):
        mk = MikroTikService(router.ip_vpn, router.user_api, router.pass_api, router.port_api)
        return await asyncio.to_thread(
            mk._request, "GET", f"/interface?type=pppoe-in&.proplist=.id,{CAMPOS_INTERFAZ}"
        )

    async def registrar(self, ahora: datetime | None = None) -> dict:
        ahora = ahora or datetime.now()
        hoy = ahora.date()
        reporte = {"servicios": 0, "routers_con_error": []}
        routers = (
            await self.db.execute(select(RouterModel).where(RouterModel.is_active == True))  # noqa: E712
        ).scalars().all()
        for router in routers:
            interfaces = await self.lector(router)
            if not isinstance(interfaces, list):
                reporte["routers_con_error"].append(router.nombre)
                continue
            muestras = muestras_por_usuario(interfaces)
            servicios = (
                await self.db.execute(
                    select(ServicioModel).where(
                        ServicioModel.router_id == router.id,
                        ServicioModel.estado != "cancelado",
                        ServicioModel.user_pppoe.isnot(None),
                    )
                )
            ).scalars().all()
            en_linea = [s for s in servicios if s.user_pppoe in muestras]
            if not en_linea:
                continue
            filas = (
                await self.db.execute(
                    select(ConsumoDiarioModel)
                    .where(
                        ConsumoDiarioModel.servicio_id.in_([s.id for s in en_linea]),
                        ConsumoDiarioModel.fecha >= hoy - timedelta(days=1),
                    )
                    .order_by(ConsumoDiarioModel.fecha)
                )
            ).scalars().all()
            ultima = {fila.servicio_id: fila for fila in filas}
            for servicio in en_linea:
                muestra = muestras[servicio.user_pppoe]
                previa = ultima.get(servicio.id)
                estado = (
                    Estado(previa.sesion_mk, previa.ultimo_rx, previa.ultimo_tx, previa.ultima_lectura)
                    if previa else None
                )
                bajada, subida, minutos = calcular_avance(estado, muestra, ahora)
                fila = previa if previa and previa.fecha == hoy else None
                if fila is None:
                    fila = ConsumoDiarioModel(
                        servicio_id=servicio.id, cliente_id=servicio.cliente_id, fecha=hoy,
                        bajada_bytes=0, subida_bytes=0, minutos_en_linea=0,
                    )
                    self.db.add(fila)
                fila.bajada_bytes = (fila.bajada_bytes or 0) + bajada
                fila.subida_bytes = (fila.subida_bytes or 0) + subida
                fila.minutos_en_linea = min((fila.minutos_en_linea or 0) + minutos, 24 * 60)
                fila.sesion_mk = muestra.sesion
                fila.ultimo_rx = muestra.rx
                fila.ultimo_tx = muestra.tx
                fila.ultima_lectura = ahora
                reporte["servicios"] += 1
            await self.db.commit()
        return reporte

    async def del_mes(self, cliente_id: int, anio: int, mes: int, servicio_id: int | None = None) -> dict:
        inicio = date(anio, mes, 1)
        fin = date(anio, mes, monthrange(anio, mes)[1])
        consulta = select(ConsumoDiarioModel).where(
            ConsumoDiarioModel.cliente_id == cliente_id,
            ConsumoDiarioModel.fecha.between(inicio, fin),
        )
        if servicio_id:
            consulta = consulta.where(ConsumoDiarioModel.servicio_id == servicio_id)
        filas = (await self.db.execute(consulta)).scalars().all()
        primero = (
            await self.db.execute(
                select(ConsumoDiarioModel.fecha)
                .where(ConsumoDiarioModel.cliente_id == cliente_id)
                .order_by(ConsumoDiarioModel.fecha)
                .limit(1)
            )
        ).scalar_one_or_none()
        return {**resumen_mes(filas, anio, mes), "registro_desde": primero.isoformat() if primero else None}

    async def limpiar(self, ahora: datetime | None = None) -> int:
        limite = (ahora or datetime.now()).date() - timedelta(days=DIAS_CONSUMO)
        resultado = await self.db.execute(delete(ConsumoDiarioModel).where(ConsumoDiarioModel.fecha < limite))
        await self.db.commit()
        return resultado.rowcount or 0
