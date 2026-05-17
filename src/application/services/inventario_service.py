from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload # 👈 Nueva importación necesaria
from src.infrastructure.models import ClienteModel, InventarioONUModel

class InventarioService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def registrar_equipo(self, identificador: str, tecnologia: str, modelo: str = "Genérico"):
        """Registra un nuevo equipo validando que no exista."""
        stmt = select(InventarioONUModel).where(InventarioONUModel.identificador == identificador)
        existe = (await self.db.execute(stmt)).scalar_one_or_none()
        
        if existe:
            raise ValueError("Esta MAC o Serial ya está registrada en el inventario.")
        
        nueva_onu = InventarioONUModel(
            identificador=identificador,
            tecnologia=tecnologia.upper(),
            modelo=modelo,
            estado="DISPONIBLE"
        )
        
        self.db.add(nueva_onu)
        await self.db.commit()
        await self.db.refresh(nueva_onu)
        
        return nueva_onu

    async def obtener_equipos(self, estado: str = None):
        """Consulta el inventario cruzando Cliente y su Zona."""
        
        # 👇 Cargamos la relación del cliente y de paso la de su zona
        stmt = select(InventarioONUModel).options(
            selectinload(InventarioONUModel.cliente).selectinload(ClienteModel.zona)
        )
        
        if estado:
            stmt = stmt.where(InventarioONUModel.estado == estado.upper())
            
        resultado = await self.db.execute(stmt)
        equipos = resultado.scalars().all()
        
        lista_final = []
        for eq in equipos:
            cliente = getattr(eq, 'cliente', None)
            # 👇 Extraemos la zona del objeto cliente
            zona_nombre = cliente.zona.nombre if (cliente and cliente.zona) else "Sin Zona"
            
            lista_final.append({
                "id": eq.id,
                "identificador": eq.identificador,
                "tecnologia": eq.tecnologia,
                "modelo": eq.modelo,
                "estado": eq.estado,
                "tecnico_id": eq.tecnico_id,
                "cliente_id": cliente.id if cliente else None,
                "cliente_nombre": cliente.nombre if cliente else None,
                "cliente_direccion": cliente.direccion if cliente else None,
                "cliente_zona": zona_nombre # 👈 ¡NUEVO DATO!
            })
            
        return lista_final

    async def eliminar_equipo(self, onu_id: int):
        """Elimina un equipo solo si no está en uso."""
        onu = await self.db.get(InventarioONUModel, onu_id)
        if not onu:
            raise ValueError("Equipo no encontrado en el inventario.")
            
        if onu.estado == "INSTALADO":
            raise ValueError("No puedes borrar un equipo que está actualmente instalado en un cliente.")
            
        await self.db.delete(onu)
        await self.db.commit()
        return "Equipo eliminado correctamente."