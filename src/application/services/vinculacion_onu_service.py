"""Vincular de golpe las ONU que reporta la OLT y que ningún cliente tiene.

Al autorizar la ONU, el técnico escribe el nombre del cliente en la
descripción. Con eso se sugiere a qué cliente pertenece cada ONU y el admin
solo confirma, en lugar de capturar el serial ficha por ficha.
"""

import difflib
import re
import unicodedata

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.application.services.ftth_service import FTTHService
from src.application.services.vsol_api_service import VsolApiService
from src.infrastructure.models import (
    ClienteModel,
    InventarioONUModel,
    OLTModel,
    ServicioModel,
    UsuarioModel,
    ZonaModel,
)

# Debajo de esto la sugerencia no se muestra; desde SEGURA se puede vincular
# junto con las demás sin revisarla una por una.
PUNTAJE_MINIMO = 0.5
PUNTAJE_SEGURO = 0.85
# Palabras que no distinguen a nadie en una descripción o un nombre.
VACIAS = {"de", "del", "la", "las", "los", "y", "onu", "cliente", "casa", "sra", "sr"}


def palabras(texto) -> list[str]:
    """"VICTOR_CONSTANTINO-Mtz." → ["victor", "constantino", "mtz"]."""
    sin_acentos = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode()
    return [p for p in re.split(r"[^a-z0-9]+", sin_acentos.lower()) if len(p) >= 2 and p not in VACIAS]


def _coincide(palabra: str, del_cliente: list[str]) -> bool:
    for otra in del_cliente:
        if palabra == otra:
            return True
        # Abreviaturas y errores de dedo: "constan" / "constantino", "matinez".
        if len(palabra) >= 4 and (otra.startswith(palabra) or palabra.startswith(otra) and len(otra) >= 4):
            return True
        if len(palabra) >= 5 and difflib.SequenceMatcher(None, palabra, otra).ratio() >= 0.85:
            return True
    return False


def puntaje(descripcion: str, cliente) -> float:
    """Qué tanto la descripción de la ONU nombra a este cliente (0 a 1)."""
    de_onu = palabras(descripcion)
    if not de_onu:
        return 0.0
    # El contrato o el usuario PPPoE escritos en la ONU la identifican sin duda.
    compacta = "".join(de_onu)
    claves = {"".join(palabras(cliente.cedula)), "".join(palabras(cliente.user_pppoe))} - {""}
    if claves & (set(de_onu) | {compacta}):
        return 1.0
    del_cliente = palabras(cliente.nombre)
    if not del_cliente:
        return 0.0
    iguales = sum(1 for p in de_onu if _coincide(p, del_cliente))
    if iguales == 0:
        return 0.0
    if iguales == 1 and len(del_cliente) > 1:
        # Un solo nombre ("Maria") no alcanza para decir de quién es.
        return min(0.45, iguales / len(de_onu))
    # Casi todo lo escrito en la ONU es del nombre, y cubre buena parte de él.
    return round(0.7 * iguales / len(de_onu) + 0.3 * min(1.0, iguales / min(len(del_cliente), 3)), 3)


def sugerir(onus: list[dict], clientes: list) -> dict[str, dict]:
    """Serial → cliente sugerido. Cada cliente se sugiere a una sola ONU."""
    pares = []
    for onu in onus:
        descripcion = onu.get("description") or ""
        puntos = sorted(((puntaje(descripcion, c), c) for c in clientes), key=lambda x: -x[0])
        if not puntos or puntos[0][0] < PUNTAJE_MINIMO:
            continue
        mejor, cliente = puntos[0]
        # Dos clientes igual de parecidos (homónimos): que decida el admin.
        empate = len(puntos) > 1 and puntos[1][0] >= mejor - 0.05
        pares.append((mejor, onu["identificador"], cliente, empate))
    resultado: dict[str, dict] = {}
    usados: set[int] = set()
    for mejor, serial, cliente, empate in sorted(pares, key=lambda x: -x[0]):
        if cliente.id in usados:
            continue
        usados.add(cliente.id)
        resultado[serial] = {
            "cliente_id": cliente.id,
            "nombre": cliente.nombre,
            "cedula": cliente.cedula,
            "puntaje": mejor,
            "segura": mejor >= PUNTAJE_SEGURO and not empate,
        }
    return resultado


class VinculacionOnuService:
    def __init__(self, db: AsyncSession, lector=None):
        self.db = db
        # lector(olt_id) -> lista unificada de ONU; se inyecta en pruebas.
        self.lector = lector or (lambda olt_id: VsolApiService(db).listar_onus_unificadas(olt_id))

    async def _clientes_sin_onu(self, olt_id: int) -> list:
        """Clientes de esa OLT (o de una zona de esa OLT) que aún no tienen ONU."""
        return (
            await self.db.execute(
                select(ClienteModel)
                .options(selectinload(ClienteModel.zona))
                .outerjoin(ZonaModel, ClienteModel.zona_id == ZonaModel.id)
                .where(
                    ClienteModel.onu_id.is_(None),
                    ClienteModel.estado != "cancelado",
                    or_(ClienteModel.olt_id == olt_id, ZonaModel.olt_id == olt_id),
                )
                .order_by(ClienteModel.nombre)
            )
        ).scalars().all()

    async def _seriales_con_dueno(self) -> set[str]:
        filas = (
            await self.db.execute(
                select(InventarioONUModel.identificador)
                .outerjoin(ClienteModel, ClienteModel.onu_id == InventarioONUModel.id)
                .outerjoin(
                    ServicioModel,
                    (ServicioModel.onu_id == InventarioONUModel.id) & (ServicioModel.estado != "cancelado"),
                )
                .where(or_(ClienteModel.id.isnot(None), ServicioModel.id.isnot(None)))
            )
        ).scalars().all()
        return {VsolApiService._normalizar_sn(s) for s in filas}

    async def propuesta(self, olt_id: int) -> dict:
        """ONU sin dueño en el sistema, con el cliente sugerido para cada una."""
        datos = await self.lector(olt_id)
        con_dueno = await self._seriales_con_dueno()
        sueltas = [
            {**onu, "identificador": VsolApiService._normalizar_sn(onu["identificador"])}
            for onu in datos.get("onus", [])
            if onu.get("identificador") and VsolApiService._normalizar_sn(onu["identificador"]) not in con_dueno
        ]
        clientes = await self._clientes_sin_onu(olt_id)
        return {
            "sueltas": [o["identificador"] for o in sueltas],
            "sugerencias": sugerir(sueltas, clientes),
            "clientes_sin_onu": [
                {"id": c.id, "nombre": c.nombre, "cedula": c.cedula, "zona": c.zona.nombre if c.zona else None}
                for c in clientes
            ],
        }

    async def vincular(self, olt_id: int, vinculos: list[dict], usuario: UsuarioModel) -> list[dict]:
        """Cada vínculo se guarda por separado: uno que falla no frena a los demás."""
        olt = await self.db.get(OLTModel, olt_id)
        if not olt:
            raise ValueError("OLT no encontrada")
        en_olt = {
            VsolApiService._normalizar_sn(o.get("identificador")): o
            for o in (await self.lector(olt_id)).get("onus", [])
            if o.get("identificador")
        }
        ftth = FTTHService(self.db)
        resultados = []
        for vinculo in vinculos:
            serial = VsolApiService._normalizar_sn(vinculo.get("identificador"))
            cliente_id = vinculo.get("cliente_id")
            try:
                onu_olt = en_olt.get(serial)
                if not onu_olt:
                    raise ValueError("La OLT ya no reporta esa ONU")
                cliente = await self.db.get(ClienteModel, cliente_id)
                if not cliente:
                    raise ValueError("El cliente no existe")
                if cliente.onu_id:
                    raise ValueError(f"{cliente.nombre} ya tiene una ONU")
                onu = (
                    await self.db.execute(
                        select(InventarioONUModel).where(InventarioONUModel.identificador == serial)
                    )
                ).scalar_one_or_none()
                if onu is None:
                    onu = InventarioONUModel(
                        identificador=serial,
                        tecnologia=(olt.tecnologia or "GPON").upper(),
                        modelo=onu_olt.get("modelo"),
                        estado="DISPONIBLE",
                    )
                    self.db.add(onu)
                    await self.db.flush()
                elif onu.estado != "DISPONIBLE":
                    # En inventario como instalada pero sin cliente (dato viejo):
                    # está en la OLT funcionando, se puede asignar.
                    onu.estado = "DISPONIBLE"
                motivo = "Vinculada desde el Radar de la OLT"
                await ftth.asignar_onu(cliente, onu.id, usuario.id, motivo=motivo)
                if not cliente.olt_id:
                    cliente.olt_id = olt.id
                servicios = (
                    await self.db.execute(
                        select(ServicioModel).where(
                            ServicioModel.cliente_id == cliente.id,
                            ServicioModel.estado != "cancelado",
                        )
                    )
                ).scalars().all()
                if len(servicios) == 1:
                    # Mismo criterio que al editar la ficha: la ONU del cliente
                    # es la de su único contrato (de ahí sale la potencia).
                    servicio = servicios[0]
                    ocupada = (
                        await self.db.execute(
                            select(ServicioModel.id).where(
                                ServicioModel.onu_id == onu.id, ServicioModel.id != servicio.id
                            )
                        )
                    ).first()
                    if not ocupada:
                        servicio.onu_id = onu.id
                    if not servicio.olt_id:
                        servicio.olt_id = olt.id
                await self.db.commit()
                resultados.append({"identificador": serial, "cliente_id": cliente_id, "ok": True})
            except Exception as exc:
                await self.db.rollback()
                resultados.append({"identificador": serial, "cliente_id": cliente_id, "ok": False, "error": str(exc)})
        return resultados
