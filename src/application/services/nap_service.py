import math
import re

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import delete, func, select
from src.infrastructure.models import (
    CajaNapModel,
    ClienteModel,
    OLTModel,
    PuertoNapModel,
    ServicioModel,
    ZonaModel,
)
from src.domain.schemas import CajaNapCreate
from sqlalchemy.orm import joinedload, selectinload
from src.application.services.ftth_service import FTTHService

# Servicios que siguen conectados a una caja (un suspendido sigue con fibra).
ESTADOS_SERVICIO_VIGENTE = ("activo", "suspendido")

RE_COORDENADAS = re.compile(r"(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)")
RADIO_TIERRA_M = 6_371_000


def parsear_coordenadas(texto):
    """'16.7521, -93.1154' (o un enlace de Maps que las traiga) → (lat, lng)."""
    if not texto:
        return None
    encontrado = RE_COORDENADAS.search(str(texto))
    if not encontrado:
        return None
    lat, lng = float(encontrado.group(1)), float(encontrado.group(2))
    if not (-90 <= lat <= 90 and -180 <= lng <= 180) or (lat == 0 and lng == 0):
        return None
    return lat, lng


def distancia_metros(a, b):
    """Distancia en línea recta (haversine) entre dos puntos (lat, lng)."""
    lat1, lng1 = map(math.radians, a)
    lat2, lng2 = map(math.radians, b)
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    )
    return 2 * RADIO_TIERRA_M * math.asin(math.sqrt(h))


def ordenar_por_cercania(origen, candidatas):
    """Ordena NAPs por distancia al domicilio.

    Cada candidata trae "posicion" (lat, lng) y "puertos_libres"; las que no
    tienen puertos libres van al final aunque estén más cerca, porque no
    sirven para conectar a nadie.
    """
    resultado = []
    for caja in candidatas:
        if not caja.get("posicion"):
            continue
        distancia = distancia_metros(origen, caja["posicion"])
        resultado.append({**caja, "distancia_m": round(distancia)})
    resultado.sort(key=lambda c: (c["puertos_libres"] <= 0, c["distancia_m"]))
    return resultado


class NapService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def listar_naps(
        self,
        zona_id: int | None = None,
        router_id: int | None = None,
        olt_id: int | None = None,
    ):
        stmt = select(CajaNapModel).options(
            selectinload(CajaNapModel.zona),
            selectinload(CajaNapModel.olt).selectinload(OLTModel.router),
        )
        if zona_id is not None:
            stmt = stmt.where(CajaNapModel.zona_id == zona_id)
        if olt_id is not None:
            stmt = stmt.where(CajaNapModel.olt_id == olt_id)
        if router_id is not None:
            stmt = stmt.where(
                CajaNapModel.olt.has(OLTModel.router_id == router_id)
            )
        stmt = stmt.order_by(CajaNapModel.nombre, CajaNapModel.id)
        
        result = await self.db.execute(stmt)
        cajas = result.scalars().all()
        
        respuesta = []
        for caja in cajas:
            await FTTHService(self.db).sincronizar_puertos_nap(caja.id)
            usados = int(
                (
                    await self.db.execute(
                        select(func.count(PuertoNapModel.id)).where(
                            PuertoNapModel.caja_nap_id == caja.id,
                            PuertoNapModel.estado == "ocupado",
                        )
                    )
                ).scalar_one()
                or 0
            )
            libres = int(
                (
                    await self.db.execute(
                        select(func.count(PuertoNapModel.id)).where(
                            PuertoNapModel.caja_nap_id == caja.id,
                            PuertoNapModel.estado == "libre",
                        )
                    )
                ).scalar_one()
                or 0
            )
            
            # 🔥 Mapeo manual 100% a prueba de fallos
            caja_dict = {
                "id": caja.id,
                "nombre": caja.nombre,
                "ubicacion": caja.ubicacion,
                "coordenadas": caja.coordenadas,
                "capacidad": caja.capacidad,
                "zona_id": caja.zona_id,
                "zona_nombre": caja.zona.nombre if caja.zona else None,
                "olt_id": caja.olt_id,
                "olt_nombre": caja.olt.nombre if caja.olt else None,
                "puerto_olt": caja.puerto_olt,
                "router_id": (
                    caja.olt.router_id if caja.olt else None
                ),
                "router_nombre": (
                    caja.olt.router.nombre
                    if caja.olt and caja.olt.router
                    else None
                ),
                "puertos_usados": usados,
                "puertos_libres": libres,
            }
            respuesta.append(caja_dict)

        await self.db.commit()
        return respuesta

    async def sugerir_naps(
        self,
        latitud: float,
        longitud: float,
        zona_id: int | None = None,
        olt_id: int | None = None,
        limite: int = 3,
    ):
        """Cajas NAP más cercanas a un domicilio."""
        candidatas = await self.candidatas(zona_id=zona_id, olt_id=olt_id)
        return self.mas_cercanas((latitud, longitud), candidatas, limite)

    @staticmethod
    def mas_cercanas(origen, candidatas, limite: int = 3):
        ordenadas = ordenar_por_cercania(origen, candidatas)[:limite]
        return [{k: v for k, v in caja.items() if k != "posicion"} for caja in ordenadas]

    async def candidatas(self, zona_id: int | None = None, olt_id: int | None = None):
        """Cajas con su posición y puertos libres, para ordenarlas por cercanía.

        Usa las coordenadas registradas de la caja; si no tiene, estima su
        posición con el promedio de los domicilios ya conectados a ella. Se
        calcula una vez y sirve para sugerir a muchos domicilios.
        """
        stmt = select(CajaNapModel).options(
            selectinload(CajaNapModel.zona),
            selectinload(CajaNapModel.olt),
        )
        if zona_id is not None:
            stmt = stmt.where(CajaNapModel.zona_id == zona_id)
        if olt_id is not None:
            stmt = stmt.where(CajaNapModel.olt_id == olt_id)
        cajas = (await self.db.execute(stmt)).scalars().all()
        if not cajas:
            return []

        sin_coordenadas = [
            c.id for c in cajas if not parsear_coordenadas(c.coordenadas)
        ]
        estimadas = {}
        if sin_coordenadas:
            for modelo, condicion in (
                (ServicioModel, ServicioModel.estado != "cancelado"),
                (ClienteModel, True),
            ):
                filas = await self.db.execute(
                    select(
                        modelo.caja_nap_id,
                        func.avg(modelo.latitud),
                        func.avg(modelo.longitud),
                    )
                    .where(
                        modelo.caja_nap_id.in_(sin_coordenadas),
                        modelo.latitud.isnot(None),
                        modelo.longitud.isnot(None),
                        modelo.latitud != 0,
                        condicion,
                    )
                    .group_by(modelo.caja_nap_id)
                )
                for caja_id, lat, lng in filas.all():
                    if lat is not None and lng is not None:
                        estimadas.setdefault(caja_id, (float(lat), float(lng)))

        candidatas = []
        for caja in cajas:
            registrada = parsear_coordenadas(caja.coordenadas)
            posicion = registrada or estimadas.get(caja.id)
            if not posicion:
                continue
            # Mismo criterio que la sincronización de puertos: servicios
            # vigentes y clientes antiguos ocupan; dañados y reservados
            # tampoco se pueden usar.
            usados = set(
                (
                    await self.db.execute(
                        select(ServicioModel.puerto_nap).where(
                            ServicioModel.caja_nap_id == caja.id,
                            ServicioModel.puerto_nap.isnot(None),
                            ServicioModel.estado != "cancelado",
                        )
                    )
                ).scalars()
            )
            usados |= set(
                (
                    await self.db.execute(
                        select(ClienteModel.puerto_nap).where(
                            ClienteModel.caja_nap_id == caja.id,
                            ClienteModel.puerto_nap.isnot(None),
                        )
                    )
                ).scalars()
            )
            usados |= set(
                (
                    await self.db.execute(
                        select(PuertoNapModel.numero).where(
                            PuertoNapModel.caja_nap_id == caja.id,
                            PuertoNapModel.estado.in_(("danado", "reservado")),
                        )
                    )
                ).scalars()
            )
            capacidad = caja.capacidad or 0
            libres = capacidad - len({p for p in usados if 1 <= p <= capacidad})
            candidatas.append(
                {
                    "id": caja.id,
                    "nombre": caja.nombre,
                    "ubicacion": caja.ubicacion,
                    "zona_id": caja.zona_id,
                    "zona_nombre": caja.zona.nombre if caja.zona else None,
                    "olt_id": caja.olt_id,
                    "olt_nombre": caja.olt.nombre if caja.olt else None,
                    "capacidad": caja.capacidad,
                    "puertos_libres": max(libres, 0),
                    "posicion": posicion,
                    "posicion_estimada": registrada is None,
                }
            )

        return candidatas

    # ------------------------------------------------ NAP de clientes viejos
    async def servicios_sin_nap(self, zona_id: int | None = None, limite_sugerencias: int = 3):
        """Servicios vigentes sin caja NAP, con las cajas más cercanas.

        Casi todos los clientes importados no tienen caja. Sin ella no se
        puede avisar a los afectados por una avería en su caja o su puerto.
        """
        stmt = (
            select(ServicioModel)
            .options(selectinload(ServicioModel.cliente), selectinload(ServicioModel.zona))
            .where(
                ServicioModel.caja_nap_id.is_(None),
                ServicioModel.estado.in_(ESTADOS_SERVICIO_VIGENTE),
            )
            .order_by(ServicioModel.zona_id, ServicioModel.id)
        )
        if zona_id is not None:
            stmt = stmt.where(ServicioModel.zona_id == zona_id)
        servicios = (await self.db.execute(stmt)).scalars().all()

        candidatas_por_zona: dict = {}
        resultado = []
        for servicio in servicios:
            gps = parsear_coordenadas(
                f"{servicio.latitud},{servicio.longitud}"
                if servicio.latitud is not None and servicio.longitud is not None
                else None
            )
            sugeridas = []
            if gps:
                if servicio.zona_id not in candidatas_por_zona:
                    candidatas_por_zona[servicio.zona_id] = await self.candidatas(zona_id=servicio.zona_id)
                sugeridas = self.mas_cercanas(gps, candidatas_por_zona[servicio.zona_id], limite_sugerencias)
            cliente = servicio.cliente
            resultado.append({
                "servicio_id": servicio.id,
                "cliente_id": servicio.cliente_id,
                "nombre": cliente.nombre if cliente else None,
                "contrato": cliente.cedula if cliente else None,
                "alias": servicio.alias,
                "direccion": servicio.direccion or (cliente.direccion if cliente else None),
                "zona_id": servicio.zona_id,
                "zona_nombre": servicio.zona.nombre if servicio.zona else None,
                "tiene_gps": bool(gps),
                "sugeridas": sugeridas,
            })
        await self.db.commit()
        return resultado

    async def asignar_nap_servicio(
        self, servicio_id: int, caja_nap_id: int, usuario_id: int, puerto_nap: int | None = None
    ):
        """Liga un servicio existente a su caja; el puerto es opcional.

        De los clientes viejos casi nunca se sabe el puerto: con la caja basta
        para avisarles de una avería. Si se da el puerto, se ocupa como en una
        instalación.
        """
        servicio = await self.db.get(ServicioModel, servicio_id)
        if not servicio or servicio.estado not in ESTADOS_SERVICIO_VIGENTE:
            raise ValueError("El servicio no existe o ya no está vigente")
        caja = await self.db.get(CajaNapModel, caja_nap_id)
        if not caja:
            raise ValueError("La caja NAP no existe")
        if puerto_nap:
            if not 1 <= puerto_nap <= (caja.capacidad or 0):
                raise ValueError(f"La caja {caja.nombre} tiene {caja.capacidad} puertos")
            await FTTHService(self.db).asignar_puerto_servicio(servicio, caja.id, puerto_nap, usuario_id)
        else:
            servicio.caja_nap_id = caja.id
            servicio.puerto_nap = None
        # El cliente guarda la caja de su servicio principal (datos heredados).
        cliente = await self.db.get(ClienteModel, servicio.cliente_id)
        if cliente and not cliente.caja_nap_id:
            cliente.caja_nap_id = caja.id
            cliente.puerto_nap = puerto_nap or None
        await self.db.commit()
        return {"servicio_id": servicio.id, "caja_nap_id": caja.id, "caja_nombre": caja.nombre,
                "puerto_nap": servicio.puerto_nap}

    # ------------------------------------------------ avisos de avería
    async def afectados_por_averia(
        self,
        caja_nap_id: int | None = None,
        olt_id: int | None = None,
        puerto_olt: int | None = None,
        zona_id: int | None = None,
    ):
        """Clientes con servicio vigente afectados por una falla.

        Por caja NAP, por puerto PON de una OLT (todas las cajas de ese puerto)
        o por zona. Uno por teléfono, aunque tenga varios servicios.
        """
        filtros = [ServicioModel.estado.in_(ESTADOS_SERVICIO_VIGENTE)]
        if caja_nap_id is not None:
            filtros.append(ServicioModel.caja_nap_id == caja_nap_id)
        elif olt_id is not None and puerto_olt is not None:
            cajas = select(CajaNapModel.id).where(
                CajaNapModel.olt_id == olt_id, CajaNapModel.puerto_olt == puerto_olt
            )
            filtros.append(ServicioModel.caja_nap_id.in_(cajas))
        elif zona_id is not None:
            filtros.append(ServicioModel.zona_id == zona_id)
        else:
            raise ValueError("Elige una caja NAP, un puerto de OLT o una zona")
        servicios = (
            await self.db.execute(
                select(ServicioModel)
                .options(selectinload(ServicioModel.cliente))
                .where(*filtros)
                .order_by(ServicioModel.id)
            )
        ).scalars().all()
        vistos = set()
        afectados = []
        for servicio in servicios:
            cliente = servicio.cliente
            telefono = re.sub(r"\D", "", (cliente.telefono or "") if cliente else "")
            if not cliente or len(telefono) < 10 or telefono[-10:] in vistos:
                continue
            vistos.add(telefono[-10:])
            afectados.append({"cliente_id": cliente.id, "nombre": cliente.nombre, "telefono": cliente.telefono})
        return afectados

    async def crear_nap(self, datos: CajaNapCreate):
        """Registra una nueva caja en la base de datos."""
        await self._validar_catalogos(datos)

        nueva_caja = CajaNapModel(
            nombre=datos.nombre,
            ubicacion=datos.ubicacion,
            coordenadas=datos.coordenadas,
            capacidad=datos.capacidad,
            zona_id=datos.zona_id,
            olt_id=datos.olt_id,
            puerto_olt=datos.puerto_olt,
        )
        self.db.add(nueva_caja)
        await self.db.flush()
        await FTTHService(self.db).sincronizar_puertos_nap(nueva_caja.id)
        await self.db.commit()
        await self.db.refresh(nueva_caja)
        
        # Agregamos valores iniciales para que el schema de respuesta no falle
        nueva_caja.puertos_usados = 0
        nueva_caja.puertos_libres = nueva_caja.capacidad
        
        return nueva_caja

    async def actualizar_nap(
        self,
        nap_id: int,
        datos: CajaNapCreate,
    ):
        caja = await self.db.get(CajaNapModel, nap_id)
        if not caja:
            raise ValueError("La caja NAP no existe")
        await self._validar_catalogos(datos)

        if datos.capacidad < caja.capacidad:
            puertos_en_uso = (
                await self.db.execute(
                    select(func.count(PuertoNapModel.id)).where(
                        PuertoNapModel.caja_nap_id == caja.id,
                        PuertoNapModel.numero > datos.capacidad,
                        PuertoNapModel.estado != "libre",
                    )
                )
            ).scalar_one()
            if puertos_en_uso:
                raise ValueError(
                    "No se puede reducir la capacidad: hay puertos superiores "
                    "al nuevo límite que están ocupados o reservados"
                )
            await self.db.execute(
                delete(PuertoNapModel).where(
                    PuertoNapModel.caja_nap_id == caja.id,
                    PuertoNapModel.numero > datos.capacidad,
                )
            )

        for campo, valor in datos.model_dump().items():
            setattr(caja, campo, valor)
        await self.db.flush()
        await FTTHService(self.db).sincronizar_puertos_nap(caja.id)
        await self.db.commit()
        await self.db.refresh(caja)
        caja.puertos_usados = (
            await self.db.execute(
                select(func.count(PuertoNapModel.id)).where(
                    PuertoNapModel.caja_nap_id == caja.id,
                    PuertoNapModel.estado == "ocupado",
                )
            )
        ).scalar_one()
        caja.puertos_libres = (
            await self.db.execute(
                select(func.count(PuertoNapModel.id)).where(
                    PuertoNapModel.caja_nap_id == caja.id,
                    PuertoNapModel.estado == "libre",
                )
            )
        ).scalar_one()
        return caja

    async def _validar_catalogos(self, datos: CajaNapCreate):
        if not await self.db.get(ZonaModel, datos.zona_id):
            raise ValueError("La zona seleccionada no existe")
        if datos.olt_id and not await self.db.get(OLTModel, datos.olt_id):
            raise ValueError("La OLT seleccionada no existe")

    async def eliminar_nap(self, nap_id: int):
        """
        Elimina una caja NAP, pero VALIDA primero que esté vacía.
        """
        # 1. Validar si hay clientes conectados
        stmt = select(func.count(ServicioModel.id)).where(
            ServicioModel.caja_nap_id == nap_id,
            ServicioModel.estado != "cancelado",
        )
        res = await self.db.execute(stmt)
        servicios_conectados = res.scalar()

        if servicios_conectados > 0:
            raise ValueError(
                "No se puede eliminar: Hay "
                f"{servicios_conectados} servicios conectados a esta NAP. "
                "Muévelos primero."
            )
        
        # 2. Buscar y eliminar
        caja = await self.db.get(CajaNapModel, nap_id)
        if not caja:
            raise ValueError("La caja NAP no existe")
        
        await self.db.delete(caja)
        await self.db.commit()
        return "Caja NAP eliminada correctamente"

    async def obtener_detalles_nap(self, nap_id: int):
        """Devuelve ocupantes por puerto, incluidos domicilios adicionales."""
        await FTTHService(self.db).sincronizar_puertos_nap(nap_id)
        stmt = (
            select(PuertoNapModel)
            .where(
                PuertoNapModel.caja_nap_id == nap_id,
                PuertoNapModel.estado == "ocupado",
            )
            .options(
                joinedload(PuertoNapModel.cliente),
                joinedload(PuertoNapModel.servicio).joinedload(
                    ServicioModel.cliente
                ),
            )
            .order_by(PuertoNapModel.numero)
        )

        puertos = (await self.db.execute(stmt)).scalars().unique().all()
        respuesta = []
        for puerto in puertos:
            servicio = puerto.servicio
            cliente = (
                servicio.cliente
                if servicio and servicio.cliente
                else puerto.cliente
            )
            if not cliente:
                continue
            nombre = cliente.nombre
            if servicio and servicio.alias:
                nombre = f"{nombre} · {servicio.alias}"
            respuesta.append(
                {
                    "id": cliente.id,
                    "nombre": nombre,
                    "cedula": cliente.cedula,
                    "puerto_nap": puerto.numero,
                }
            )
        await self.db.commit()
        return respuesta
