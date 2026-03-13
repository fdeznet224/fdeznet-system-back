from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from src.infrastructure.models import RouterModel

class RouterRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_all(self):
        result = await self.db.execute(select(RouterModel))
        return result.scalars().all()
    
    async def get_by_id(self, router_id: int):
        return await self.db.get(RouterModel, router_id)