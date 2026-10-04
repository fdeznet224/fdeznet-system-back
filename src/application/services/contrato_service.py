"""Números de contrato: generación única y contratos apartados por técnico.

El contrato es la credencial del cliente (4 hexadecimales, ej. "A7F2"). Antes
nacía al crear el cliente; ahora un técnico puede tener contratos apartados
para escribirlo en el conector de la caja NAP antes de dar de alta.
"""

import random
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.models import ClienteModel, ContratoReservadoModel

CARACTERES_CONTRATO = "0123456789ABCDEF"
LARGO_CONTRATO = 4
APARTADOS_POR_TECNICO = 5
ROLES_CUALQUIER_APARTADO = {"admin", "supervisor"}
# Un apartado que no se usó en este tiempo se libera (el número no se reutiliza).
VIGENCIA_APARTADO = timedelta(days=30)


async def codigo_disponible(db: AsyncSession, codigo: str) -> bool:
    """Ni un cliente ni un apartado (aunque sea viejo) tiene ese código."""
    cliente = await db.scalar(select(ClienteModel.id).where(ClienteModel.cedula == codigo).limit(1))
    if cliente:
        return False
    apartado = await db.scalar(
        select(ContratoReservadoModel.id).where(ContratoReservadoModel.codigo == codigo).limit(1)
    )
    return not apartado


async def generar_codigo_contrato(db: AsyncSession) -> str:
    while True:
        codigo = "".join(random.choices(CARACTERES_CONTRATO, k=LARGO_CONTRATO))
        if await codigo_disponible(db, codigo):
            return codigo


class ContratoService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def _vigentes(self, usuario_id: int):
        return (
            await self.db.execute(
                select(ContratoReservadoModel)
                .where(
                    ContratoReservadoModel.usuario_id == usuario_id,
                    ContratoReservadoModel.usado_en.is_(None),
                    ContratoReservadoModel.liberado_en.is_(None),
                )
                .order_by(ContratoReservadoModel.reservado_en, ContratoReservadoModel.id)
            )
        ).scalars().all()

    async def apartados(self, usuario_id: int, cantidad: int = APARTADOS_POR_TECNICO):
        """Los apartados del técnico, completando hasta `cantidad`."""
        ahora = datetime.now()
        vigentes = []
        for apartado in await self._vigentes(usuario_id):
            if ahora - apartado.reservado_en > VIGENCIA_APARTADO:
                apartado.liberado_en = ahora
            else:
                vigentes.append(apartado)
        while len(vigentes) < cantidad:
            nuevo = ContratoReservadoModel(
                codigo=await generar_codigo_contrato(self.db), usuario_id=usuario_id, reservado_en=ahora,
            )
            self.db.add(nuevo)
            # El índice único en "codigo" impide que dos técnicos reciban el
            # mismo aunque lo pidan a la vez: la segunda escritura falla.
            await self.db.flush()
            vigentes.append(nuevo)
        await self.db.commit()
        return vigentes

    async def descartar(self, usuario_id: int, codigo: str) -> None:
        apartado = await self._apartado_vigente(usuario_id, codigo)
        apartado.liberado_en = datetime.now()
        await self.db.commit()

    async def _apartado_vigente(self, usuario_id: int | None, codigo: str) -> ContratoReservadoModel:
        """El apartado con ese código; si se indica usuario, debe ser suyo."""
        consulta = select(ContratoReservadoModel).where(
            ContratoReservadoModel.codigo == (codigo or "").strip().upper()
        )
        if usuario_id is not None:
            consulta = consulta.where(ContratoReservadoModel.usuario_id == usuario_id)
        apartado = await self.db.scalar(consulta)
        if not apartado:
            raise ValueError(
                "Ese contrato no está apartado a tu nombre"
                if usuario_id is not None
                else "Ese contrato no está apartado a ningún técnico"
            )
        if apartado.usado_en:
            raise ValueError("Ese contrato ya se usó en otro cliente")
        if apartado.liberado_en:
            raise ValueError("Ese contrato ya fue liberado; usa otro de tus apartados")
        return apartado

    async def usar(self, usuario, codigo: str, cliente_id: int) -> str:
        """Marca el apartado como usado por ese cliente (sin hacer commit).

        El técnico solo usa los suyos; el admin o supervisor puede dar de alta
        con el contrato que un técnico ya escribió en el conector.
        """
        dueno = None if getattr(usuario, "rol", None) in ROLES_CUALQUIER_APARTADO else usuario.id
        apartado = await self._apartado_vigente(dueno, codigo)
        apartado.usado_en = datetime.now()
        apartado.cliente_id = cliente_id
        return apartado.codigo
