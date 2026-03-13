from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from src.infrastructure.models import RouterModel, PlanModel, ClienteModel, ConfiguracionModel
from src.domain.schemas import RouterCreate, PlanCreate, ClienteCreate
from src.utils.text_tools import limpiar_string_para_usuario

class RouterRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_routers(self):
        result = await self.db.execute(select(RouterModel))
        return result.scalars().all()

    async def create_router(self, router: RouterCreate):
        db_router = RouterModel(**router.dict())
        self.db.add(db_router)
        await self.db.commit()
        await self.db.refresh(db_router)
        return db_router

class PlanRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create_plan(self, plan: PlanCreate):
        db_plan = PlanModel(**plan.dict())
        self.db.add(db_plan)
        await self.db.commit()
        await self.db.refresh(db_plan)
        return db_plan

    async def get_all_planes(self):
        result = await self.db.execute(select(PlanModel))
        return result.scalars().all()

    async def get_planes_by_router(self, router_id: int):
        result = await self.db.execute(
            select(PlanModel).where(PlanModel.router_id == router_id)
        )
        return result.scalars().all()

class ClienteRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    # 👇👇👇 ESTE ES EL MÉTODO QUE TE FALTA - ASEGÚRATE DE QUE ESTÉ AQUÍ 👇👇👇
    async def get_by_id(self, cliente_id: int):
        stmt = (
            select(ClienteModel)
            .options(
                selectinload(ClienteModel.plan),
                selectinload(ClienteModel.router),
                selectinload(ClienteModel.plantilla),
                selectinload(ClienteModel.zona),
                selectinload(ClienteModel.caja_nap) 
            )
            .where(ClienteModel.id == cliente_id)
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()
    # 👆👆👆

    async def create_cliente(self, cliente: ClienteCreate):
        # 1. Procesamiento de usuario (limpieza)
        if not cliente.user_pppoe:
             cliente.user_pppoe = limpiar_string_para_usuario(cliente.nombre)
        
        # 2. Procesamiento de password (default)
        if not cliente.pass_pppoe or not cliente.pass_pppoe.strip():
            stmt = select(ConfiguracionModel).where(ConfiguracionModel.clave == 'pppoe_password_default')
            res = await self.db.execute(stmt)
            config_db = res.scalar()
            cliente.pass_pppoe = config_db.valor if config_db else "12345"
            
        # 3. Asignar estado por defecto
        if not cliente.estado:
            cliente.estado = "activo"

        # 4. Creación del objeto 
        db_cliente = ClienteModel(**cliente.dict())

        self.db.add(db_cliente)
        await self.db.commit()
        
        # 5. Devolver objeto completo (usando el método que acabamos de crear)
        return await self.get_by_id(db_cliente.id)

    async def get_clientes(self):
        result = await self.db.execute(
            select(ClienteModel).options(
                selectinload(ClienteModel.router),
                selectinload(ClienteModel.plan),
                selectinload(ClienteModel.zona),
                selectinload(ClienteModel.plantilla),
                selectinload(ClienteModel.caja_nap)
            ).order_by(ClienteModel.id.desc())
        )
        return result.scalars().all()
        
    async def get_clientes_by_router(self, router_id: int):
        result = await self.db.execute(select(ClienteModel).where(ClienteModel.router_id == router_id))
        return result.scalars().all()