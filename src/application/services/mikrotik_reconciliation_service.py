import asyncio
from collections import Counter, defaultdict
from typing import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from src.infrastructure.mikrotik_service import MikroTikService
from src.infrastructure.models import (
    LogCronjobModel,
    ServicioModel,
)


ESTADOS_CONCILIABLES = {"activo", "suspendido"}
ORIGEN_LOG = "ConciliacionMikroTik"


class MikrotikReconciliationService:
    """Hace que MikroTik converja al estado deseado guardado en la BD."""

    def __init__(
        self,
        db: AsyncSession,
        mikrotik_factory: Callable = MikroTikService,
        blocking_runner: Callable = asyncio.to_thread,
    ):
        self.db = db
        self.mikrotik_factory = mikrotik_factory
        self.blocking_runner = blocking_runner

    @staticmethod
    def _es_verdadero(valor) -> bool:
        return str(valor).strip().lower() in {
            "true",
            "yes",
            "1",
            "si",
        }

    @staticmethod
    def _valor_limpio(valor) -> str:
        return str(valor or "").strip()

    @classmethod
    def _configuracion_desviada(cls, servicio, secret) -> bool:
        if not secret:
            return True
        if cls._valor_limpio(secret.get("profile")) != cls._valor_limpio(
            servicio.plan.nombre
        ):
            return True
        if cls._valor_limpio(
            secret.get("remote-address")
        ) != cls._valor_limpio(servicio.ip_asignada):
            return True
        password_real = secret.get("password")
        return (
            password_real is not None
            and str(password_real) != str(servicio.pass_pppoe)
        )

    @staticmethod
    def _descripcion(servicio) -> str:
        return (
            f"servicio={servicio.id} "
            f"cliente={servicio.cliente_id} "
            f"alias='{servicio.alias}' "
            f"pppoe='{servicio.user_pppoe}'"
        )

    def _registrar_log(self, nivel: str, mensaje: str):
        self.db.add(
            LogCronjobModel(
                nivel=nivel,
                origen=ORIGEN_LOG,
                mensaje=mensaje,
            )
        )

    async def _cargar_servicios(self):
        return (
            await self.db.execute(
                select(ServicioModel)
                .options(
                    joinedload(ServicioModel.router),
                    joinedload(ServicioModel.plan),
                    joinedload(ServicioModel.cliente),
                )
                .where(
                    ServicioModel.estado.in_(ESTADOS_CONCILIABLES)
                )
                .order_by(
                    ServicioModel.router_id,
                    ServicioModel.id,
                )
            )
        ).scalars().unique().all()

    @staticmethod
    def _validar_configuracion(servicio):
        faltantes = []
        if not servicio.router:
            faltantes.append("router")
        elif not servicio.router.is_active:
            faltantes.append("router activo")
        if not servicio.plan:
            faltantes.append("plan")
        if not servicio.user_pppoe:
            faltantes.append("usuario PPPoE")
        if not servicio.pass_pppoe:
            faltantes.append("contraseña PPPoE")
        if not servicio.ip_asignada:
            faltantes.append("IP")
        if faltantes:
            raise ValueError(
                "configuración incompleta: " + ", ".join(faltantes)
            )

    async def _reconciliar_servicio(
        self,
        mk,
        servicio,
        secrets_por_usuario,
        ips_cortadas,
    ) -> list[str]:
        self._validar_configuracion(servicio)
        usuario = servicio.user_pppoe
        secret = secrets_por_usuario.get(usuario)
        acciones = []

        if self._configuracion_desviada(servicio, secret):
            await self.blocking_runner(
                mk.crear_actualizar_pppoe,
                servicio.user_pppoe,
                servicio.pass_pppoe,
                servicio.plan.nombre,
                servicio.ip_asignada,
                (
                    f"{servicio.cliente.nombre} | "
                    f"Servicio:{servicio.id} {servicio.alias}"
                ),
            )
            acciones.append(
                "crear/actualizar secret PPPoE"
                if secret
                else "crear secret PPPoE faltante"
            )
            secret = await self.blocking_runner(
                mk.obtener_pppoe_estricto,
                usuario,
            )
            if not secret:
                raise RuntimeError(
                    "MikroTik no confirmó el secret PPPoE"
                )
            secrets_por_usuario[usuario] = secret

        debe_suspender = servicio.estado == "suspendido"
        esta_deshabilitado = self._es_verdadero(
            secret.get("disabled")
        )
        if esta_deshabilitado != debe_suspender:
            encontrado = await self.blocking_runner(
                mk.activar_desactivar_pppoe,
                usuario,
                debe_suspender,
            )
            if encontrado is not True:
                raise RuntimeError(
                    "MikroTik no confirmó el cambio de estado PPPoE"
                )
            acciones.append(
                "deshabilitar PPPoE"
                if debe_suspender
                else "habilitar PPPoE"
            )

        esta_en_corte = servicio.ip_asignada in ips_cortadas
        if esta_en_corte != debe_suspender:
            confirmado = await self.blocking_runner(
                mk.gestionar_corte_cliente,
                servicio.ip_asignada,
                debe_suspender,
            )
            if confirmado is not True:
                raise RuntimeError(
                    "MikroTik no confirmó la lista de corte"
                )
            if debe_suspender:
                ips_cortadas.add(servicio.ip_asignada)
                acciones.append("agregar IP a CORTE_FDEZNET")
            else:
                ips_cortadas.discard(servicio.ip_asignada)
                acciones.append("retirar IP de CORTE_FDEZNET")

        if acciones:
            verificado = await self.blocking_runner(
                mk.obtener_pppoe_estricto,
                usuario,
            )
            if not verificado:
                raise RuntimeError(
                    "el secret desapareció durante la verificación"
                )
            if (
                self._es_verdadero(verificado.get("disabled"))
                != debe_suspender
            ):
                raise RuntimeError(
                    "el estado PPPoE final no coincide con la BD"
                )

        if debe_suspender:
            servicio.is_online = False
        return acciones

    async def ejecutar(self) -> dict[str, int]:
        servicios = await self._cargar_servicios()
        por_router = defaultdict(list)
        reporte = {
            "verificados": len(servicios),
            "correctos": 0,
            "reparados": 0,
            "errores": 0,
            "routers": 0,
        }
        claves_pppoe = Counter(
            (
                servicio.router_id,
                self._valor_limpio(servicio.user_pppoe),
            )
            for servicio in servicios
            if servicio.router_id and servicio.user_pppoe
        )

        for servicio in servicios:
            clave_pppoe = (
                servicio.router_id,
                self._valor_limpio(servicio.user_pppoe),
            )
            if (
                servicio.router_id
                and servicio.user_pppoe
                and claves_pppoe[clave_pppoe] > 1
            ):
                reporte["errores"] += 1
                self._registrar_log(
                    "ERROR",
                    (
                        f"No conciliado {self._descripcion(servicio)}: "
                        "el usuario PPPoE está repetido en el mismo router; "
                        "se requiere corrección manual"
                    ),
                )
            elif servicio.router_id:
                por_router[servicio.router_id].append(servicio)
            else:
                reporte["errores"] += 1
                self._registrar_log(
                    "ERROR",
                    (
                        f"No conciliado {self._descripcion(servicio)}: "
                        "configuración incompleta: router"
                    ),
                )

        for servicios_router in por_router.values():
            router = servicios_router[0].router
            reporte["routers"] += 1
            if not router or not router.is_active:
                for servicio in servicios_router:
                    reporte["errores"] += 1
                    self._registrar_log(
                        "ERROR",
                        (
                            f"No conciliado {self._descripcion(servicio)}: "
                            "router inexistente o inactivo"
                        ),
                    )
                continue

            mk = self.mikrotik_factory(
                router.ip_vpn,
                router.user_api,
                router.pass_api,
                router.port_api,
            )
            try:
                secrets = await self.blocking_runner(
                    mk.obtener_todos_pppoe_estricto
                )
                ips_cortadas = await self.blocking_runner(
                    mk.obtener_ips_cortadas
                )
                secrets_por_usuario = {
                    item.get("name"): item
                    for item in secrets
                    if item.get("name")
                }
            except Exception as exc:
                for servicio in servicios_router:
                    reporte["errores"] += 1
                    self._registrar_log(
                        "ERROR",
                        (
                            f"Falló verificación en router "
                            f"'{router.nombre}' para "
                            f"{self._descripcion(servicio)}; "
                            f"se reintentará: {exc}"
                        ),
                    )
                continue

            for servicio in servicios_router:
                try:
                    acciones = await self._reconciliar_servicio(
                        mk,
                        servicio,
                        secrets_por_usuario,
                        ips_cortadas,
                    )
                    if acciones:
                        reporte["reparados"] += 1
                        self._registrar_log(
                            "WARNING",
                            (
                                f"Deriva reparada en router "
                                f"'{router.nombre}' para "
                                f"{self._descripcion(servicio)}: "
                                + "; ".join(acciones)
                            ),
                        )
                    else:
                        reporte["correctos"] += 1
                except Exception as exc:
                    reporte["errores"] += 1
                    accion = (
                        "suspender"
                        if servicio.estado == "suspendido"
                        else "activar"
                    )
                    self._registrar_log(
                        "ERROR",
                        (
                            f"Falló comando '{accion}' en router "
                            f"'{router.nombre}' para "
                            f"{self._descripcion(servicio)}; "
                            f"se reintentará: {exc}"
                        ),
                    )

        nivel = "ERROR" if reporte["errores"] else "INFO"
        self._registrar_log(
            nivel,
            (
                "Ciclo completado: "
                f"{reporte['verificados']} verificados, "
                f"{reporte['correctos']} correctos, "
                f"{reporte['reparados']} reparados, "
                f"{reporte['errores']} con error."
            ),
        )
        await self.db.commit()
        return reporte
