from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload # 👈 Nueva importación necesaria
from src.infrastructure.models import ClienteModel, InventarioONUModel
from src.application.services.ftth_service import FTTHService

class InventarioService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def registrar_equipo(
        self,
        identificador: str,
        tecnologia: str,
        modelo: str = "Genérico",
        usuario_id: int = None,
    ):
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
        await self.db.flush()
        FTTHService(self.db).registrar_movimiento(
            onu=nueva_onu,
            tecnico_id=usuario_id,
            tipo_movimiento="alta_inventario",
            estado_anterior=None,
            estado_nuevo="DISPONIBLE",
            condicion="NUEVA",
            motivo="Alta de equipo en inventario",
        )
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

    async def eliminar_equipo(self, onu_id: int, usuario_id: int = None):
        """Da de baja un equipo conservando todo su historial."""
        onu = await self.db.get(InventarioONUModel, onu_id)
        if not onu:
            raise ValueError("Equipo no encontrado en el inventario.")
            
        if onu.estado in {"INSTALADO", "RESERVADO", "POR_RECOGER"}:
            raise ValueError(
                "No puedes dar de baja un equipo instalado, reservado "
                "o pendiente de recoger."
            )

        estado_anterior = onu.estado
        onu.estado = "BAJA"
        onu.tecnico_id = None
        FTTHService(self.db).registrar_movimiento(
            onu=onu,
            tecnico_id=usuario_id,
            tipo_movimiento="baja_inventario",
            estado_anterior=estado_anterior,
            estado_nuevo="BAJA",
            motivo="Baja administrativa de inventario",
        )
        await self.db.commit()
        return "Equipo dado de baja; su historial fue conservado."
