from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from src.infrastructure.models import OLTModel 
from src.domain import schemas 

class OLTRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_all(self):
        result = await self.db.execute(select(OLTModel))
        return result.scalars().all()

    async def get_by_id(self, olt_id: int):
        result = await self.db.execute(select(OLTModel).filter(OLTModel.id == olt_id))
        return result.scalars().first()

    async def get_by_ip(self, ip: str):
        result = await self.db.execute(select(OLTModel).filter(OLTModel.ip == ip))
        return result.scalars().first()

    async def create(self, olt_data: schemas.OLTCreate):
        db_olt = OLTModel(**olt_data.model_dump()) 
        self.db.add(db_olt)
        await self.db.commit()
        await self.db.refresh(db_olt)
        return db_olt

    async def update(self, db_olt: OLTModel, olt_data: schemas.OLTUpdate):
        update_data = olt_data.model_dump(exclude_unset=True)
        for key, value in update_data.items():
            setattr(db_olt, key, value)
        await self.db.commit()
        await self.db.refresh(db_olt)
        return db_olt

    async def delete(self, db_olt: OLTModel):
        await self.db.delete(db_olt)
        await self.db.commit()
        return True