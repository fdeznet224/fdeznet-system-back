from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from src.infrastructure.repositories.olt_repository import OLTRepository
from src.domain import schemas 

class OLTService:
    def __init__(self, db: AsyncSession):
        self.repository = OLTRepository(db)

    async def obtener_todas(self):
        return await self.repository.get_all()

    async def obtener_por_id(self, olt_id: int):
        olt = await self.repository.get_by_id(olt_id)
        if not olt:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, 
                detail="OLT no encontrada"
            )
        return olt

    async def crear_olt(self, olt_data: schemas.OLTCreate):
        if await self.repository.get_by_ip(olt_data.ip):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, 
                detail="Ya existe una OLT registrada con esta dirección IP"
            )
        return await self.repository.create(olt_data)

    async def actualizar_olt(self, olt_id: int, olt_data: schemas.OLTUpdate):
        olt = await self.obtener_por_id(olt_id)
        
        if olt_data.ip and olt_data.ip != olt.ip:
            if await self.repository.get_by_ip(olt_data.ip):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, 
                    detail="La nueva dirección IP ya está en uso por otra OLT"
                )
                
        return await self.repository.update(olt, olt_data)

    async def eliminar_olt(self, olt_id: int):
        olt = await self.obtener_por_id(olt_id)
        return await self.repository.delete(olt)