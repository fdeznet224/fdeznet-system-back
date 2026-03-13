from typing import List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

# Modelos y Schemas
from src.infrastructure.models import PlantillaFacturacionModel
from src.domain.schemas import BillingTemplateRequest

class BillingTemplateService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_all(self) -> List[PlantillaFacturacionModel]:
        """Obtiene todas las plantillas de facturación"""
        result = await self.db.execute(select(PlantillaFacturacionModel))
        return result.scalars().all()

    async def get_by_id(self, id: int) -> Optional[PlantillaFacturacionModel]:
        """Busca una plantilla por ID"""
        return await self.db.get(PlantillaFacturacionModel, id)

    async def create(self, data: BillingTemplateRequest) -> PlantillaFacturacionModel:
        """
        Crea una nueva plantilla.
        Gracias a que unificamos el Schema con el Modelo, 
        podemos desempaquetar el diccionario directamente (**data.dict()).
        """
        # Convertimos el Schema a Modelo DB directamente
        nuevo = PlantillaFacturacionModel(**data.dict())
        
        self.db.add(nuevo)
        await self.db.commit()
        await self.db.refresh(nuevo)
        return nuevo

    async def update(self, id: int, data: BillingTemplateRequest) -> Optional[PlantillaFacturacionModel]:
        """Actualiza una plantilla existente"""
        plantilla = await self.get_by_id(id)
        if not plantilla:
            return None

        # Actualización dinámica (Iteramos sobre los campos del schema)
        for key, value in data.dict().items():
            setattr(plantilla, key, value)

        await self.db.commit()
        await self.db.refresh(plantilla)
        return plantilla

    async def delete(self, id: int) -> bool:
        """Elimina una plantilla"""
        plantilla = await self.get_by_id(id)
        if not plantilla:
            return False
        
        # NOTA: Si hay clientes usando esta plantilla, SQLAlchemy lanzará error de integridad.
        # Es recomendable validar antes si 'plantilla.clientes' está vacío.
        
        await self.db.delete(plantilla)
        await self.db.commit()
        return True