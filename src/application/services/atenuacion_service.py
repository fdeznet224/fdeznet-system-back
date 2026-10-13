"""Atenuación: clientes y troncales cuya señal óptica bajó.

Cada cliente se compara contra su propia señal normal (la mediana de sus
lecturas de la última semana, sin el último día), nunca contra sus vecinos:
en una red desbalanceada (Paraíso, La Merced) cada cliente tiene un nivel
distinto por diseño. Si varios clientes de la misma troncal (PON), caja NAP o
zona bajan al mismo tiempo, el problema está en la fibra antes de ellos.
"""

from collections import defaultdict
from datetime import datetime, timedelta
from statistics import median

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.infrastructure.models import CajaNapModel, LecturaOpticaModel, OLTModel, ServicioModel, ZonaModel

DIAS_NORMAL = 7
# La señal "de ahora" es la mediana de las lecturas de estas últimas horas:
# una lectura rara de una hora no cuenta como baja.
HORAS_ACTUAL = 3
# Lo normal no incluye el último día, para que una baja de hoy no se
# promedie consigo misma.
EXCLUIR_DEL_NORMAL = timedelta(hours=24)
MINIMO_LECTURAS_NORMAL = 6
BAJA_DB = 1.5
BAJA_FUERTE_DB = 3.0
# Un grupo se atenuó si bajaron al menos 2 clientes y la mitad de los medidos.
MINIMO_BAJARON = 2
PROPORCION_BAJARON = 0.5


def nivel_de(cambio: float) -> str:
    if cambio <= -BAJA_FUERTE_DB:
        return "bajo_fuerte"
    if cambio <= -BAJA_DB:
        return "bajo"
    return "estable"


def coordenadas(texto: str | None) -> tuple[float, float] | None:
    """"16.395358, -92.693243" → (16.395358, -92.693243)."""
    try:
        lat, lng = (float(parte) for parte in str(texto or "").split(","))
    except ValueError:
        return None
    return (lat, lng) if -90 <= lat <= 90 and -180 <= lng <= 180 else None


def cambios_por_servicio(lecturas, ahora: datetime) -> dict[int, dict]:
    """lecturas: (servicio_id, rx, fecha, olt_id, pon) → cambio de cada servicio."""
    desde_actual = ahora - timedelta(hours=HORAS_ACTUAL)
    hasta_normal = ahora - EXCLUIR_DEL_NORMAL
    por_servicio: dict[int, dict] = defaultdict(lambda: {"actual": [], "normal": [], "olt_id": None, "pon": None, "fecha": None})
    for servicio_id, rx, fecha, olt_id, pon in lecturas:
        datos = por_servicio[servicio_id]
        if fecha >= desde_actual:
            datos["actual"].append(float(rx))
        elif fecha <= hasta_normal:
            datos["normal"].append(float(rx))
        if datos["fecha"] is None or fecha >= datos["fecha"]:
            datos["fecha"] = fecha
            datos["olt_id"] = olt_id or datos["olt_id"]
            datos["pon"] = pon if pon is not None else datos["pon"]
    resultado = {}
    for servicio_id, datos in por_servicio.items():
        if not datos["actual"] or len(datos["normal"]) < MINIMO_LECTURAS_NORMAL:
            continue
        actual = round(median(datos["actual"]), 2)
        normal = round(median(datos["normal"]), 2)
        cambio = round(actual - normal, 2)
        resultado[servicio_id] = {
            "actual": actual, "normal": normal, "cambio_db": cambio, "nivel": nivel_de(cambio),
            "olt_id": datos["olt_id"], "pon": datos["pon"],
        }
    return resultado


def agrupar(miembros: list[dict]) -> dict | None:
    """Resumen de un grupo; None si no se atenuó."""
    medidos = len(miembros)
    bajaron = [m for m in miembros if m["cambio_db"] <= -BAJA_DB]
    if len(bajaron) < MINIMO_BAJARON or len(bajaron) / medidos < PROPORCION_BAJARON:
        return None
    return {
        "medidos": medidos,
        "bajaron": len(bajaron),
        "cambio_mediano_db": round(median(m["cambio_db"] for m in bajaron), 2),
        "peor_db": min(m["cambio_db"] for m in bajaron),
        "cliente_ids": sorted({m["cliente_id"] for m in bajaron}),
    }


class AtenuacionService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def analizar(self, ahora: datetime | None = None) -> dict:
        ahora = ahora or datetime.now()
        lecturas = (
            await self.db.execute(
                select(
                    LecturaOpticaModel.servicio_id,
                    LecturaOpticaModel.potencia_rx_dbm,
                    LecturaOpticaModel.fecha,
                    LecturaOpticaModel.olt_id,
                    LecturaOpticaModel.pon,
                ).where(
                    LecturaOpticaModel.fecha >= ahora - timedelta(days=DIAS_NORMAL),
                    LecturaOpticaModel.servicio_id.isnot(None),
                    LecturaOpticaModel.origen == "automatica",
                )
            )
        ).all()
        cambios = cambios_por_servicio(lecturas, ahora)
        if not cambios:
            return {"clientes": {}, "grupos": [], "umbral_db": BAJA_DB, "calculado_en": ahora.isoformat()}

        servicios = (
            await self.db.execute(
                select(ServicioModel)
                .options(selectinload(ServicioModel.cliente))
                .where(ServicioModel.id.in_(cambios))
            )
        ).scalars().all()
        olts = {o.id: o.nombre for o in (await self.db.execute(select(OLTModel))).scalars().all()}
        zonas = {z.id: z.nombre for z in (await self.db.execute(select(ZonaModel))).scalars().all()}
        naps = {n.id: n for n in (await self.db.execute(select(CajaNapModel))).scalars().all()}

        clientes: dict[int, dict] = {}
        grupos: dict[tuple, list[dict]] = defaultdict(list)
        for servicio in servicios:
            cliente = servicio.cliente
            if not cliente:
                continue
            dato = cambios[servicio.id]
            lat = servicio.latitud or cliente.latitud
            lng = servicio.longitud or cliente.longitud
            miembro = {
                "cliente_id": cliente.id,
                "cambio_db": dato["cambio_db"],
                "ubicacion": (lat, lng) if lat and lng else None,
            }
            # Con varios domicilios cuenta el que más bajó.
            if cliente.id not in clientes or dato["cambio_db"] < clientes[cliente.id]["cambio_db"]:
                clientes[cliente.id] = {k: dato[k] for k in ("actual", "normal", "cambio_db", "nivel")}
            olt_id = dato["olt_id"] or servicio.olt_id or cliente.olt_id
            if olt_id:
                grupos[("pon", olt_id, dato["pon"])].append(miembro)
            caja_nap_id = servicio.caja_nap_id or cliente.caja_nap_id
            if caja_nap_id:
                grupos[("nap", caja_nap_id)].append(miembro)
            if cliente.zona_id:
                grupos[("zona", cliente.zona_id)].append(miembro)

        resultado = []
        for clave, miembros in grupos.items():
            resumen = agrupar(miembros)
            if not resumen:
                continue
            tipo = clave[0]
            ubicacion = None
            if tipo == "pon":
                olt = olts.get(clave[1], f"OLT {clave[1]}")
                nombre = f"PON {clave[2]} · {olt}" if clave[2] is not None else f"OLT {olt}"
            elif tipo == "nap":
                nap = naps.get(clave[1])
                nombre = f"Caja {nap.nombre}" if nap else f"Caja NAP {clave[1]}"
                ubicacion = coordenadas(nap.coordenadas) if nap else None
            else:
                nombre = f"Zona {zonas.get(clave[1], clave[1])}"
            if ubicacion is None:
                puntos = [m["ubicacion"] for m in miembros if m["ubicacion"] and m["cliente_id"] in resumen["cliente_ids"]]
                if puntos:
                    ubicacion = (sum(p[0] for p in puntos) / len(puntos), sum(p[1] for p in puntos) / len(puntos))
            resultado.append({
                "tipo": tipo,
                "nombre": nombre,
                "ubicacion": list(ubicacion) if ubicacion else None,
                **resumen,
            })
        resultado.sort(key=lambda g: (g["cambio_mediano_db"], -g["bajaron"]))
        return {"clientes": clientes, "grupos": resultado, "umbral_db": BAJA_DB, "calculado_en": ahora.isoformat()}
