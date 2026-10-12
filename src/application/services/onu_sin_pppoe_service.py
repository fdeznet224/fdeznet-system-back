"""ONU en línea en la OLT cuyo cliente no tiene sesión PPPoE en su MikroTik.

Pasa tras algunos apagones: la ONU registra con buena potencia pero su
PPPoE nunca llega al MikroTik, aunque el equipo esté sano. Se arregla
borrándola de la OLT para que se autorice desde cero (reautorizar).
"""

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.application.services.cache_olt import olvidar
from src.application.services.descripcion_onu_service import (
    DescripcionOnuService,
    texto_descripcion,
    usa_api_vsol,
)
from src.application.services.vsol_api_service import VsolApiService
from src.infrastructure.mikrotik_service import MikroTikService
from src.infrastructure.models import (
    ClienteModel,
    LogActividadModel,
    LogCronjobModel,
    OLTModel,
    RouterModel,
)

logger = logging.getLogger(__name__)

# Una sola pasada sin sesión puede ser un reinicio normal del cliente.
MINIMO_SIN_PPPOE = timedelta(minutes=15)
ESPERA_REGISTRO_S = 180
ESPERA_PPPOE_S = 120
INTERVALO_S = 15

# Las tareas en segundo plano se guardan para que no las recoja el recolector.
_seguimientos: set = set()


def pendientes_de_aviso(detectados: dict, vistos: dict, avisados: set, ahora: datetime) -> list:
    """Casos que ya llevan MINIMO_SIN_PPPOE sin sesión y aún no se avisaron.

    `vistos` y `avisados` se conservan entre pasadas; un caso que se arregla
    sale de ambos y, si vuelve a pasar, se avisa de nuevo.
    """
    for cliente_id in list(vistos):
        if cliente_id not in detectados:
            vistos.pop(cliente_id)
            avisados.discard(cliente_id)
    nuevos = []
    for cliente_id, caso in detectados.items():
        desde = vistos.setdefault(cliente_id, ahora)
        if cliente_id not in avisados and ahora - desde >= MINIMO_SIN_PPPOE:
            avisados.add(cliente_id)
            nuevos.append(caso)
    return nuevos


def mensaje_aviso(casos: list) -> str:
    lineas = [
        f"⚠️ ONU en línea pero sin internet (sin PPPoE): {len(casos)} cliente(s)",
        "",
    ]
    for caso in casos:
        potencia = f" · {caso['rx']} dBm" if caso.get("rx") not in (None, "", "N/A") else ""
        lineas.append(f"• {caso['nombre']} · {caso['olt']} {caso['ubicacion']}{potencia}")
    lineas += [
        "",
        "Suele pasar tras un apagón y el equipo está bien. En la ficha del cliente: "
        "Herramientas → Reautorizar en la OLT.",
    ]
    return "\n".join(lineas)


class OnuSinPppoeService:
    def __init__(self, db: AsyncSession, vsol: VsolApiService | None = None):
        self.db = db
        self.vsol = vsol or VsolApiService(db)

    async def _sesiones(self, router_id: int, cache: dict):
        """Usuarios PPPoE conectados; None si el MikroTik no respondió."""
        if router_id in cache:
            return cache[router_id]
        router = await self.db.get(RouterModel, router_id)
        activos = None
        if router and router.is_active:
            mk = MikroTikService(router.ip_vpn, router.user_api, router.pass_api, router.port_api)
            try:
                respuesta = await asyncio.to_thread(mk._request, "GET", "/ppp/active", None, True)
                if isinstance(respuesta, list) and respuesta:
                    activos = {s.get("name") for s in respuesta if s.get("name")}
            except Exception as exc:
                logger.warning("MikroTik %s no respondió: %s", router.nombre, exc)
        cache[router_id] = activos
        return activos

    async def detectar(self) -> dict:
        """cliente_id → caso, para clientes activos con ONU en línea y sin sesión."""
        olts = [
            o for o in (await self.db.execute(select(OLTModel).order_by(OLTModel.id))).scalars().all()
            if usa_api_vsol(o)
        ]
        clientes = await DescripcionOnuService(self.db, self.vsol)._clientes_por_onu()
        sesiones: dict = {}
        detectados = {}
        for olt in olts:
            try:
                onus = (await self.vsol.listar_onus_unificadas(olt.id)).get("onus", [])
            except Exception as exc:
                logger.warning("No se pudo leer la OLT %s: %s", olt.nombre, exc)
                continue
            for onu in onus:
                if str(onu.get("estado_fisico")) != "online":
                    continue
                cliente = clientes.get(VsolApiService._normalizar_sn(onu.get("identificador")))
                if not cliente or cliente.estado != "activo" or not cliente.user_pppoe or not cliente.router_id:
                    continue
                activos = await self._sesiones(cliente.router_id, sesiones)
                if activos is None or cliente.user_pppoe in activos:
                    continue
                detectados[cliente.id] = {
                    "cliente_id": cliente.id,
                    "nombre": cliente.nombre,
                    "olt": olt.nombre,
                    "ubicacion": str(onu.get("onu_id") or ""),
                    "serial": onu.get("identificador"),
                    "rx": onu.get("rx_power"),
                }
        return detectados

    async def reautorizar(self, cliente_id: int, usuario=None) -> dict:
        """Borra de la OLT la ONU del cliente para que se autorice desde cero."""
        cliente = (
            await self.db.execute(
                select(ClienteModel).options(selectinload(ClienteModel.onu_asignada)).where(ClienteModel.id == cliente_id)
            )
        ).scalar_one_or_none()
        if not cliente:
            raise ValueError("Cliente no encontrado.")
        serial = VsolApiService._normalizar_sn(cliente.onu_asignada.identificador if cliente.onu_asignada else None)
        olt = await self.db.get(OLTModel, cliente.olt_id) if cliente.olt_id else None
        if not olt or not serial:
            raise ValueError("Al cliente le falta la OLT o el serial de su ONU.")
        if not olt.api_enabled:
            raise ValueError(f"La OLT {olt.nombre} no permite cambios desde el sistema.")

        olvidar(f"vsol:{olt.id}")
        onus = (await self.vsol.listar_onus_unificadas(olt.id)).get("onus", [])
        actual = next((o for o in onus if VsolApiService._normalizar_sn(o.get("identificador")) == serial), None)
        ubicacion = VsolApiService.ubicacion_onu(actual) if actual else None
        if not ubicacion:
            raise ValueError("La OLT no tiene registrada esa ONU; revisa el serial o que esté conectada.")
        if str(actual.get("estado_fisico")) != "online":
            raise ValueError(
                "La ONU está apagada en la OLT: reautorizarla no sirve. Revisa la luz y la fibra del cliente."
            )
        pon, onuid = ubicacion
        await asyncio.to_thread(self.vsol._eliminar_onu_sync, olt, pon, onuid, serial)
        olvidar(f"vsol:{olt.id}")
        self.db.add(
            LogActividadModel(
                usuario_id=getattr(usuario, "id", None),
                usuario_nombre=getattr(usuario, "usuario", None),
                accion="Reautorización de ONU en la OLT desde la ficha del cliente",
                metodo="POST",
                ruta=f"/api/clientes/{cliente.id}/reautorizar-onu",
                estado_http=200,
                detalle=f"Cliente {cliente.id} · ONU {serial} · borrada de GPON0/{pon}:{onuid}",
            )
        )
        await self.db.commit()

        tarea = asyncio.create_task(seguir_reautorizacion(cliente.id, olt.id, serial))
        _seguimientos.add(tarea)
        tarea.add_done_callback(_seguimientos.discard)
        return {"olt": olt.nombre, "serial": serial, "ubicacion_anterior": f"GPON0/{pon}:{onuid}"}


async def seguir_reautorizacion(cliente_id: int, olt_id: int, serial: str, dormir=asyncio.sleep, sesiones=None) -> str:
    """Espera a que la ONU vuelva, le pone el nombre y confirma su PPPoE; deja el resultado en la bitácora."""
    from src.infrastructure.database import SessionLocal

    async with (sesiones or SessionLocal)() as db:
        vsol = VsolApiService(db)
        olt = await db.get(OLTModel, olt_id)
        cliente = await db.get(ClienteModel, cliente_id)
        ubicacion = None
        for _ in range(ESPERA_REGISTRO_S // INTERVALO_S):
            await dormir(INTERVALO_S)
            olvidar(f"vsol:{olt.id}")
            try:
                onus = (await vsol.listar_onus_unificadas(olt.id)).get("onus", [])
            except Exception:
                continue
            onu = next((o for o in onus if VsolApiService._normalizar_sn(o.get("identificador")) == serial), None)
            if onu and str(onu.get("estado_fisico")) == "online":
                ubicacion = VsolApiService.ubicacion_onu(onu)
                break

        if not ubicacion:
            mensaje, nivel = (
                f"{cliente.nombre}: la ONU {serial} no volvió a autorizarse en {ESPERA_REGISTRO_S // 60} min. "
                "Revisa que la OLT tenga la autorización automática activa.",
                "WARN",
            )
        else:
            pon, onuid = ubicacion
            try:
                await asyncio.to_thread(
                    vsol._escribir_descripciones_sync, olt,
                    [(pon, onuid, serial, texto_descripcion(cliente.nombre, cliente.cedula))],
                )
            except Exception as exc:
                logger.warning("No se pudo poner el nombre a la ONU %s: %s", serial, exc)
            sesion = {"online": False}
            router = await db.get(RouterModel, cliente.router_id) if cliente.router_id else None
            if router and cliente.user_pppoe:
                mk = MikroTikService(router.ip_vpn, router.user_api, router.pass_api, router.port_api)
                for _ in range(ESPERA_PPPOE_S // INTERVALO_S):
                    await dormir(INTERVALO_S)
                    sesion = await asyncio.to_thread(mk.obtener_info_sesion, cliente.user_pppoe)
                    if sesion.get("online"):
                        break
            if sesion.get("online"):
                mensaje, nivel = (
                    f"{cliente.nombre}: ONU {serial} reautorizada en GPON0/{pon}:{onuid} y PPPoE conectado "
                    f"({sesion.get('ip')}).",
                    "INFO",
                )
            else:
                mensaje, nivel = (
                    f"{cliente.nombre}: ONU {serial} reautorizada en GPON0/{pon}:{onuid}, pero sin PPPoE "
                    f"tras {ESPERA_PPPOE_S // 60} min. Revisa su usuario y contraseña PPPoE o la configuración de la ONU.",
                    "WARN",
                )
        db.add(LogCronjobModel(nivel=nivel, origen="ReautorizarONU", mensaje=mensaje[:1000]))
        await db.commit()
        return mensaje
