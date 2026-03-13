from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
# Usamos selectinload para carga de relaciones asíncronas eficiente
from sqlalchemy.orm import selectinload 
from typing import Optional

from src.infrastructure.models import ClienteModel, ConfiguracionModel
from src.domain.schemas import ClienteCreate
from src.utils.text_tools import limpiar_string_para_usuario

class ClienteRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    # 👇 1. RENOMBRADO: De 'get_cliente_by_id' a 'get_by_id' (Lo que pide el servicio)
    async def get_by_id(self, cliente_id: int):
        query = select(ClienteModel).options(
            selectinload(ClienteModel.router),
            selectinload(ClienteModel.plan),
            selectinload(ClienteModel.zona),
            selectinload(ClienteModel.plantilla),
            selectinload(ClienteModel.caja_nap) # 👈 NECESARIO PARA VER LA CAJA
        ).where(ClienteModel.id == cliente_id)
        
        result = await self.db.execute(query)
        return result.scalar_one_or_none()

    async def get_all_clientes(self, router_id: Optional[int] = None):
        query = select(ClienteModel).options(
            selectinload(ClienteModel.router),
            selectinload(ClienteModel.plan),
            selectinload(ClienteModel.zona),
            selectinload(ClienteModel.plantilla),
            selectinload(ClienteModel.caja_nap)
        )
        if router_id:
            query = query.where(ClienteModel.router_id == router_id)
        
        query = query.order_by(desc(ClienteModel.id))
        result = await self.db.execute(query)
        return result.scalars().all()
    
    async def existe_ip(self, ip: str) -> bool:
        stmt = select(ClienteModel).where(ClienteModel.ip_asignada == ip)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def create_cliente(self, datos: ClienteCreate):
        # 2. LOGICA DE NEGOCIO (Limpieza de usuario y pass por defecto)
        
        # Si no hay usuario PPPoE, usar el nombre limpio
        if not datos.user_pppoe:
             datos.user_pppoe = limpiar_string_para_usuario(datos.nombre)
        
        # Si no hay contraseña, buscar la default en config
        if not datos.pass_pppoe or not datos.pass_pppoe.strip():
            stmt = select(ConfiguracionModel).where(ConfiguracionModel.clave == 'pppoe_password_default')
            res = await self.db.execute(stmt)
            config_db = res.scalar()
            datos.pass_pppoe = config_db.valor if config_db else "12345"

        # 3. CREACIÓN (Soporta todos los campos nuevos como cedula, caja_nap_id)
        nuevo_cliente = ClienteModel(**datos.dict())
        self.db.add(nuevo_cliente)
        await self.db.commit()
        
        # 4. Retornar usando el método corregido
        return await self.get_by_id(nuevo_cliente.id)