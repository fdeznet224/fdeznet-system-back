from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, and_
from sqlalchemy.orm import joinedload
from typing import Optional, List
from datetime import date

from src.infrastructure.models import FacturaModel, ClienteModel

class FacturaRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_factura_by_id(self, factura_id: int) -> Optional[FacturaModel]:
        """Obtiene una factura con toda la información de su cliente, plan y router."""
        query = select(FacturaModel).options(
            joinedload(FacturaModel.cliente).joinedload(ClienteModel.plan),
            joinedload(FacturaModel.cliente).joinedload(ClienteModel.router)
        ).where(FacturaModel.id == factura_id)
        
        result = await self.db.execute(query)
        return result.scalar_one_or_none()

    async def get_facturas_filtradas(
        self, 
        zona_id: Optional[int] = None, 
        router_id: Optional[int] = None, 
        estado: Optional[str] = None,
        limit: int = 100
    ) -> List[FacturaModel]:
        """
        Consulta avanzada con filtros cruzados. 
        Permite ver facturas por Zona o Router uniendo con la tabla Clientes.
        """
        query = select(FacturaModel).options(
            joinedload(FacturaModel.cliente).joinedload(ClienteModel.zona),
            joinedload(FacturaModel.cliente).joinedload(ClienteModel.router)
        ).join(ClienteModel)

        # Aplicación de filtros dinámicos
        filtros = []
        if zona_id:
            filtros.append(ClienteModel.zona_id == zona_id)
        if router_id:
            filtros.append(ClienteModel.router_id == router_id)
        if estado:
            filtros.append(FacturaModel.estado == estado)

        if filtros:
            query = query.where(and_(*filtros))

        query = query.order_by(desc(FacturaModel.id)).limit(limit)
        result = await self.db.execute(query)
        return result.scalars().all()

    async def existe_factura_mes(self, cliente_id: int, mes_correspondiente: str) -> bool:
        """Verifica si un cliente ya tiene factura para un mes específico (evita duplicados)."""
        query = select(FacturaModel).where(
            and_(
                FacturaModel.cliente_id == cliente_id,
                FacturaModel.mes_correspondiente == mes_correspondiente,
                FacturaModel.estado != "anulada"
            )
        )
        result = await self.db.execute(query)
        return result.scalar_one_or_none() is not None

    async def create_factura(self, factura: FacturaModel) -> FacturaModel:
        """Persiste una nueva factura en la base de datos."""
        self.db.add(factura)
        await self.db.commit()
        await self.db.refresh(factura)
        return factura