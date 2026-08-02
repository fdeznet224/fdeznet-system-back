from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import delete, func, select
from src.infrastructure.models import (
    CajaNapModel,
    OLTModel,
    PuertoNapModel,
    ServicioModel,
    ZonaModel,
)
from src.domain.schemas import CajaNapCreate
from sqlalchemy.orm import joinedload, selectinload
from src.application.services.ftth_service import FTTHService

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
            stmt_count = select(
                func.sum(PuertoNapModel.estado == "ocupado"),
                func.sum(PuertoNapModel.estado == "libre"),
            ).where(PuertoNapModel.caja_nap_id == caja.id)
            usados, libres = (await self.db.execute(stmt_count)).one()
            usados = int(usados or 0)
            libres = int(libres or 0)
            
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
