from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from src.infrastructure.models import PlanModel
from src.domain.schemas import PlanCreate

class PlanRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_all_planes(self):
        result = await self.db.execute(select(PlanModel))
        return result.scalars().all()

    async def get_planes_by_router(self, router_id: int):
        stmt = select(PlanModel).where(PlanModel.router_id == router_id)
        result = await self.db.execute(stmt)
        return result.scalars().all()

    async def create_plan(self, plan: PlanCreate):
        db_plan = PlanModel(**plan.dict())
        self.db.add(db_plan)
        await self.db.commit()
        await self.db.refresh(db_plan)
        return db_plan