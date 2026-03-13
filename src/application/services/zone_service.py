from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

# Modelos y Schemas
from src.infrastructure.models import ZonaModel, ClienteModel
from src.domain.schemas import ZonaCreate

class ZoneService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def listar_zonas(self):
        """Devuelve todas las zonas ordenadas alfabéticamente"""
        stmt = select(ZonaModel).order_by(ZonaModel.nombre)
        result = await self.db.execute(stmt)
        return result.scalars().all()

    async def crear_zona(self, datos: ZonaCreate):
        """Crea una nueva zona"""
        # Validación opcional: Verificar si ya existe el nombre
        # stmt = select(ZonaModel).where(ZonaModel.nombre == datos.nombre) ...
        
        nueva = ZonaModel(nombre=datos.nombre)
        self.db.add(nueva)
        await self.db.commit()
        await self.db.refresh(nueva)
        return nueva

    async def editar_zona(self, zona_id: int, datos: ZonaCreate):
        """Edita el nombre de una zona existente"""
        zona = await self.db.get(ZonaModel, zona_id)
        if not zona:
            raise ValueError("Zona no encontrada")

        zona.nombre = datos.nombre
        
        await self.db.commit()
        await self.db.refresh(zona)
        return zona

    async def eliminar_zona(self, zona_id: int):
        """Elimina una zona si no tiene clientes asignados"""
        zona = await self.db.get(ZonaModel, zona_id)
        if not zona:
            raise ValueError("Zona no encontrada")

        # 1. Verificar Integridad: ¿Hay clientes en esta zona?
        stmt = select(func.count(ClienteModel.id)).where(ClienteModel.zona_id == zona_id)
        res = await self.db.execute(stmt)
        clientes_en_zona = res.scalar()

        if clientes_en_zona > 0:
            raise ValueError(f"No se puede eliminar: Hay {clientes_en_zona} clientes asignados a esta zona.")

        # 2. Eliminar
        await self.db.delete(zona)
        await self.db.commit()
        return "Zona eliminada correctamente"