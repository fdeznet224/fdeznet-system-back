from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from src.infrastructure.models import CajaNapModel, ClienteModel
from src.domain.schemas import CajaNapCreate
from sqlalchemy.orm import selectinload

class NapService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def listar_naps(self, zona_id: int = None):
        """
        Lista todas las cajas NAP, calculando en tiempo real
        cuántos puertos están ocupados y cuántos libres.
        """
        stmt = select(CajaNapModel)
        if zona_id:
            stmt = stmt.where(CajaNapModel.zona_id == zona_id)
        
        result = await self.db.execute(stmt)
        cajas = result.scalars().all()
        
        respuesta = []
        for caja in cajas:
            # Contar clientes conectados a esta caja específica
            stmt_count = select(func.count(ClienteModel.id)).where(ClienteModel.caja_nap_id == caja.id)
            usados_res = await self.db.execute(stmt_count)
            usados = usados_res.scalar() or 0
            
            # Convertimos el objeto ORM a diccionario para agregar campos calculados
            caja_dict = caja.__dict__.copy() # Usamos copy para no afectar la sesión
            caja_dict['puertos_usados'] = usados
            caja_dict['puertos_libres'] = caja.capacidad - usados
            
            respuesta.append(caja_dict)
            
        return respuesta

    async def crear_nap(self, datos: CajaNapCreate):
        """Registra una nueva caja en la base de datos."""
        nueva_caja = CajaNapModel(
            nombre=datos.nombre,
            ubicacion=datos.ubicacion,
            coordenadas=datos.coordenadas,
            capacidad=datos.capacidad,
            zona_id=datos.zona_id
        )
        self.db.add(nueva_caja)
        await self.db.commit()
        await self.db.refresh(nueva_caja)
        
        # Agregamos valores iniciales para que el schema de respuesta no falle
        nueva_caja.puertos_usados = 0
        nueva_caja.puertos_libres = nueva_caja.capacidad
        
        return nueva_caja

    async def eliminar_nap(self, nap_id: int):
        """
        Elimina una caja NAP, pero VALIDA primero que esté vacía.
        """
        # 1. Validar si hay clientes conectados
        stmt = select(func.count(ClienteModel.id)).where(ClienteModel.caja_nap_id == nap_id)
        res = await self.db.execute(stmt)
        clientes_conectados = res.scalar()

        if clientes_conectados > 0:
            raise ValueError(f"No se puede eliminar: Hay {clientes_conectados} clientes conectados a esta NAP. Muévelos primero.")
        
        # 2. Buscar y eliminar
        caja = await self.db.get(CajaNapModel, nap_id)
        if not caja:
            raise ValueError("La caja NAP no existe")
        
        await self.db.delete(caja)
        await self.db.commit()
        return "Caja NAP eliminada correctamente"

    async def obtener_detalles_nap(self, nap_id: int):
        """
        Devuelve la lista de clientes conectados para pintar el diagrama visual.
        Usamos selectinload para evitar el error 'MissingGreenlet' al serializar.
        """
        stmt = (
            select(ClienteModel)
            .where(ClienteModel.caja_nap_id == nap_id)
            .options(
                # Cargamos todas las relaciones que usa tu ClienteResponse
                selectinload(ClienteModel.router),
                selectinload(ClienteModel.zona),
                selectinload(ClienteModel.plan),
                selectinload(ClienteModel.plantilla),
                selectinload(ClienteModel.caja_nap) 
            )
        )
        
        result = await self.db.execute(stmt)
        return result.scalars().all()