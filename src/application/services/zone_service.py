from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

# Modelos y Schemas
from src.infrastructure.models import OLTModel, PlantillaFacturacionModel, RouterModel, ServicioModel, ZonaModel
from src.domain.schemas import ZonaCreate

class ZoneService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def listar_zonas(self):
        """Devuelve todas las zonas ordenadas alfabéticamente"""
        stmt = select(ZonaModel).order_by(ZonaModel.nombre)
        result = await self.db.execute(stmt)
        return result.scalars().all()

    async def _infraestructura(self, datos: ZonaCreate) -> dict:
        """Valida MikroTik, OLT y plantilla de la zona y los deja coherentes."""
        router_id, olt_id, plantilla_id = datos.router_id, datos.olt_id, datos.plantilla_id
        if olt_id:
            olt = await self.db.get(OLTModel, olt_id)
            if not olt:
                raise ValueError("La OLT seleccionada no existe")
            if olt.router_id and router_id and olt.router_id != router_id:
                raise ValueError("La OLT seleccionada está conectada a otro MikroTik")
            router_id = router_id or olt.router_id
        if router_id and not await self.db.get(RouterModel, router_id):
            raise ValueError("El MikroTik seleccionado no existe")
        if plantilla_id and not await self.db.get(PlantillaFacturacionModel, plantilla_id):
            raise ValueError("La plantilla de cobro seleccionada no existe")
        return {"router_id": router_id, "olt_id": olt_id, "plantilla_id": plantilla_id}

    async def crear_zona(self, datos: ZonaCreate):
        """Crea una nueva zona con la infraestructura que la atiende."""
        nueva = ZonaModel(nombre=datos.nombre.strip(), **await self._infraestructura(datos))
        self.db.add(nueva)
        await self.db.commit()
        await self.db.refresh(nueva)
        return nueva

    async def editar_zona(self, zona_id: int, datos: ZonaCreate):
        """Edita el nombre y la infraestructura de una zona."""
        zona = await self.db.get(ZonaModel, zona_id)
        if not zona:
            raise LookupError("Zona no encontrada")

        zona.nombre = datos.nombre.strip()
        for campo, valor in (await self._infraestructura(datos)).items():
            setattr(zona, campo, valor)

        await self.db.commit()
        await self.db.refresh(zona)
        return zona

    async def eliminar_zona(self, zona_id: int):
        """Elimina una zona si no tiene clientes asignados"""
        zona = await self.db.get(ZonaModel, zona_id)
        if not zona:
            raise ValueError("Zona no encontrada")

        # 1. Verificar Integridad: ¿Hay clientes en esta zona?
        stmt = select(func.count(ServicioModel.id)).where(
            ServicioModel.zona_id == zona_id,
            ServicioModel.estado != "cancelado",
        )
        res = await self.db.execute(stmt)
        servicios_en_zona = res.scalar()

        if servicios_en_zona > 0:
            raise ValueError(
                "No se puede eliminar: Hay "
                f"{servicios_en_zona} servicios asignados a esta zona."
            )

        # 2. Eliminar
        await self.db.delete(zona)
        await self.db.commit()
        return "Zona eliminada correctamente"
