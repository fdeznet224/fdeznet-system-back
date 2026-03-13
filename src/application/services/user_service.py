from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from passlib.context import CryptContext

# Importamos Modelos y Schemas
from src.infrastructure.models import UsuarioModel, RouterModel
from src.domain.schemas import UsuarioCreate, UsuarioUpdate

# Configuración de Hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

class UserService:
    def __init__(self, db: AsyncSession):
        self.db = db

    def get_password_hash(self, password):
        return pwd_context.hash(password)

    # ==========================================
    # CREAR USUARIO
    # ==========================================
    async def crear_usuario(self, datos: UsuarioCreate):
        # 1. Validar duplicados
        stmt = select(UsuarioModel).where(UsuarioModel.usuario == datos.usuario)
        res = await self.db.execute(stmt)
        if res.scalar_one_or_none():
            raise ValueError(f"El usuario '{datos.usuario}' ya existe.")

        # 2. Crear instancia base
        nuevo_usuario = UsuarioModel(
            nombre_completo=datos.nombre_completo,
            usuario=datos.usuario,
            rol=datos.rol,
            activo=datos.activo,
            password_hash=self.get_password_hash(datos.password)
        )

        # 3. Asignar Routers Permitidos (Relación Many-to-Many)
        if datos.router_ids:
            stmt_r = select(RouterModel).where(RouterModel.id.in_(datos.router_ids))
            result_r = await self.db.execute(stmt_r)
            routers_reales = result_r.scalars().all()
            
            nuevo_usuario.routers_asignados = routers_reales
        
        # 4. Guardar
        self.db.add(nuevo_usuario)
        await self.db.commit()
        await self.db.refresh(nuevo_usuario)
        return nuevo_usuario

    # ==========================================
    # EDITAR USUARIO
    # ==========================================
    async def editar_usuario(self, user_id: int, datos: UsuarioUpdate):
        # 1. Buscar Usuario con sus routers
        stmt = select(UsuarioModel).options(
            selectinload(UsuarioModel.routers_asignados)
        ).where(UsuarioModel.id == user_id)
        
        result = await self.db.execute(stmt)
        usuario_db = result.scalar_one_or_none()

        if not usuario_db:
            raise ValueError("Usuario no encontrado")

        # 2. Actualizar campos simples
        if datos.nombre_completo is not None: 
            usuario_db.nombre_completo = datos.nombre_completo
        
        if datos.usuario is not None: usuario_db.usuario = datos.usuario
        if datos.rol is not None: usuario_db.rol = datos.rol
        if datos.activo is not None: usuario_db.activo = datos.activo
        
        # 3. Actualizar Contraseña (si se envió)
        if datos.password and len(datos.password) > 0:
            usuario_db.password_hash = self.get_password_hash(datos.password)

        # 4. Actualizar Permisos de Routers
        if datos.router_ids is not None:
            # Buscamos los nuevos routers seleccionados
            stmt_r = select(RouterModel).where(RouterModel.id.in_(datos.router_ids))
            result_r = await self.db.execute(stmt_r)
            nuevos_routers = result_r.scalars().all()
            
            # Reemplazamos la lista completa
            usuario_db.routers_asignados = nuevos_routers

        await self.db.commit()
        await self.db.refresh(usuario_db)
        return usuario_db

    # ==========================================
    # LISTAR Y ELIMINAR
    # ==========================================
    async def listar_usuarios(self):
        # Traemos también los routers asignados para mostrarlos en el frontend
        stmt = select(UsuarioModel).options(
            selectinload(UsuarioModel.routers_asignados)
        ).order_by(UsuarioModel.id)
        
        result = await self.db.execute(stmt)
        return result.scalars().all()

    async def eliminar_usuario(self, user_id: int):
        usuario_db = await self.db.get(UsuarioModel, user_id)
        if not usuario_db:
            return False # Indicamos que no se encontró
        
        await self.db.delete(usuario_db)
        await self.db.commit()
        return True