import asyncio
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
from types import SimpleNamespace
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from src.application.services.orden_service import OrdenService
from src.application.services.snmp_service import SNMPMonitorService
from src.application.services.vsol_api_service import VsolApiService
from src.infrastructure.mikrotik_service import MikroTikService
from src.infrastructure.models import (
    ClienteModel,
    DiagnosticoSoporteModel,
    LecturaOpticaModel,
    OrdenServicioModel,
    UsuarioModel,
)


CATEGORIAS_SOPORTE = {
    "sin_internet",
    "lentitud",
    "potencia_baja",
    "router_wifi",
    "cable_roto",
    "cambio_domicilio",
    "otro",
}
CANALES_REPORTE = {"panel", "telefono", "whatsapp", "presencial", "monitoreo"}
ESTADOS_ABIERTOS = {"pendiente", "asignada", "en_camino", "trabajando"}


class SupportService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def crear_incidencia(
        self,
        cliente_id: int,
        categoria: str,
        descripcion: str,
        usuario: UsuarioModel,
        tecnico_id: Optional[int] = None,
        prioridad: Optional[str] = None,
        fecha_programada: Optional[datetime] = None,
        canal_reporte: str = "panel",
        commit: bool = True,
    ):
        categoria = self.normalizar_categoria(categoria)
        canal = (canal_reporte or "panel").strip().lower()
        if canal not in CANALES_REPORTE:
            raise ValueError("Canal de reporte inválido")
        if len((descripcion or "").strip()) < 5:
            raise ValueError("Describe brevemente la incidencia")

        cliente = (
            await self.db.execute(
                select(ClienteModel)
                .where(ClienteModel.id == cliente_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if not cliente or cliente.estado == "eliminado":
            raise ValueError("Cliente no encontrado o eliminado")

        duplicada = (
            await self.db.execute(
                select(OrdenServicioModel.id).where(
                    OrdenServicioModel.cliente_id == cliente_id,
                    OrdenServicioModel.categoria_soporte == categoria,
                    OrdenServicioModel.estado.in_(ESTADOS_ABIERTOS),
                ).limit(1)
            )
        ).scalar_one_or_none()
        if duplicada:
            raise RuntimeError(
                f"Ya existe la incidencia abierta #{duplicada} para esa categoría"
            )

        tipo = "cambio_domicilio" if categoria == "cambio_domicilio" else "reparacion"
        prioridad_final = (
            (prioridad or "").strip().lower()
            or self.prioridad_sugerida(categoria)
        )
        datos = SimpleNamespace(
            tipo=tipo,
            cliente_id=cliente_id,
            prospecto_nombre=None,
            prospecto_telefono=None,
            prospecto_direccion=None,
            tecnico_id=tecnico_id,
            prioridad=prioridad_final,
            fecha_programada=fecha_programada,
            motivo=categoria,
            descripcion=descripcion.strip(),
        )
        orden_service = OrdenService(self.db)
        orden = await orden_service.crear(datos, usuario, commit=False)
        orden.categoria_soporte = categoria
        orden.canal_reporte = canal
        if commit:
            await self.db.commit()
        else:
            await self.db.flush()
        return await orden_service.obtener(orden.id, usuario)

    async def bandeja(
        self,
        usuario: UsuarioModel,
        estado: Optional[str] = None,
        categoria: Optional[str] = None,
        prioridad: Optional[str] = None,
        solo_vencidas: bool = False,
        limite: int = 100,
    ):
        stmt = (
            OrdenService._consulta_base()
            .where(
                OrdenServicioModel.tipo.in_(["reparacion", "cambio_domicilio"]),
                OrdenServicioModel.categoria_soporte.isnot(None),
            )
            .options(
                selectinload(OrdenServicioModel.diagnosticos_soporte)
                .selectinload(DiagnosticoSoporteModel.ejecutado_por)
            )
        )
        if usuario.rol == "tecnico":
            stmt = stmt.where(OrdenServicioModel.tecnico_id == usuario.id)
        if estado:
            stmt = stmt.where(OrdenServicioModel.estado == estado)
        if categoria:
            stmt = stmt.where(
                OrdenServicioModel.categoria_soporte
                == self.normalizar_categoria(categoria)
            )
        if prioridad:
            stmt = stmt.where(OrdenServicioModel.prioridad == prioridad)
        if solo_vencidas:
            stmt = stmt.where(
                OrdenServicioModel.estado.in_(ESTADOS_ABIERTOS),
                OrdenServicioModel.fecha_programada < datetime.now(),
            )
        return (
            await self.db.execute(
                stmt.order_by(
                    (OrdenServicioModel.prioridad == "urgente").desc(),
                    (OrdenServicioModel.prioridad == "alta").desc(),
                    OrdenServicioModel.fecha_programada.is_(None),
                    OrdenServicioModel.fecha_programada,
                    OrdenServicioModel.created_at,
                ).limit(limite)
            )
        ).scalars().unique().all()

    async def ejecutar_diagnostico(
        self,
        orden_id: int,
        usuario: UsuarioModel,
    ) -> DiagnosticoSoporteModel:
        orden = await OrdenService(self.db).obtener(orden_id, usuario)
        if orden.tipo not in {"reparacion", "cambio_domicilio"}:
            raise ValueError("La orden no corresponde a soporte")
        if orden.estado in {"terminada", "cancelada"}:
            raise ValueError("No se puede diagnosticar una orden cerrada")
        if not orden.cliente_id:
            raise ValueError("La orden no tiene cliente asociado")

        cliente = (
            await self.db.execute(
                select(ClienteModel)
                .options(
                    joinedload(ClienteModel.router),
                    joinedload(ClienteModel.olt),
                    joinedload(ClienteModel.onu_asignada),
                )
                .where(ClienteModel.id == orden.cliente_id)
            )
        ).scalar_one()

        mikrotik, olt = await asyncio.gather(
            self._diagnosticar_mikrotik(cliente),
            self._diagnosticar_olt(cliente),
        )
        clasificacion = self.clasificar(
            categoria=orden.categoria_soporte or "otro",
            estado_cliente=cliente.estado,
            mikrotik=mikrotik,
            olt=olt,
        )
        errores = [
            item
            for item in [mikrotik.get("error"), olt.get("error")]
            if item
        ]
        registro = DiagnosticoSoporteModel(
            orden_id=orden.id,
            cliente_id=cliente.id,
            ejecutado_por_id=usuario.id,
            resultado=clasificacion["resultado"],
            codigo_sugerencia=clasificacion["codigo"],
            sugerencia=clasificacion["sugerencia"],
            mikrotik_disponible=bool(mikrotik.get("disponible")),
            pppoe_online=mikrotik.get("pppoe_online"),
            ip_actual=mikrotik.get("ip_actual"),
            uptime=mikrotik.get("uptime"),
            mac_reportada=mikrotik.get("mac_reportada"),
            ping_estado=mikrotik.get("ping_estado"),
            perdida_paquetes_porcentaje=mikrotik.get("perdida_porcentaje"),
            trafico_subida_bps=mikrotik.get("subida_bps"),
            trafico_bajada_bps=mikrotik.get("bajada_bps"),
            olt_disponible=bool(olt.get("disponible")),
            onu_online=olt.get("onu_online"),
            potencia_rx_dbm=olt.get("potencia_rx_dbm"),
            potencia_tx_dbm=olt.get("potencia_tx_dbm"),
            origen_olt=olt.get("origen"),
            errores="\n".join(errores) or None,
            datos_crudos=json.dumps(
                {"mikrotik": mikrotik, "olt": olt},
                ensure_ascii=False,
                default=str,
            )[:16000],
        )
        self.db.add(registro)
        await self.db.flush()

        orden.diagnostico = self.resumen_texto(registro)
        orden.version += 1
        if registro.potencia_rx_dbm is not None and cliente.onu_id:
            self.db.add(
                LecturaOpticaModel(
                    cliente_id=cliente.id,
                    onu_id=cliente.onu_id,
                    orden_id=orden.id,
                    tecnico_id=usuario.id,
                    potencia_rx_dbm=registro.potencia_rx_dbm,
                    potencia_tx_dbm=registro.potencia_tx_dbm,
                    origen=registro.origen_olt or "diagnostico",
                    observaciones=(
                        f"Diagnóstico de soporte #{registro.id}: "
                        f"{registro.codigo_sugerencia}"
                    ),
                )
            )
        await self.db.commit()
        await self.db.refresh(registro)
        return registro

    async def listar_diagnosticos(
        self,
        orden_id: int,
        usuario: UsuarioModel,
    ):
        await OrdenService(self.db).obtener(orden_id, usuario)
        return (
            await self.db.execute(
                select(DiagnosticoSoporteModel)
                .options(selectinload(DiagnosticoSoporteModel.ejecutado_por))
                .where(DiagnosticoSoporteModel.orden_id == orden_id)
                .order_by(DiagnosticoSoporteModel.fecha.desc())
            )
        ).scalars().all()

    async def metricas(self, desde: date, hasta: date):
        stmt = select(
            func.count(OrdenServicioModel.id),
            func.avg(OrdenServicioModel.tiempo_primera_respuesta_minutos),
            func.avg(OrdenServicioModel.tiempo_resolucion_minutos),
            func.sum(
                (OrdenServicioModel.estado == "terminada")
            ),
        ).where(
            OrdenServicioModel.categoria_soporte.isnot(None),
            func.date(OrdenServicioModel.created_at) >= desde,
            func.date(OrdenServicioModel.created_at) <= hasta,
        )
        total, promedio_respuesta, promedio_resolucion, terminadas = (
            await self.db.execute(stmt)
        ).one()
        por_categoria = (
            await self.db.execute(
                select(
                    OrdenServicioModel.categoria_soporte,
                    func.count(OrdenServicioModel.id),
                )
                .where(
                    OrdenServicioModel.categoria_soporte.isnot(None),
                    func.date(OrdenServicioModel.created_at) >= desde,
                    func.date(OrdenServicioModel.created_at) <= hasta,
                )
                .group_by(OrdenServicioModel.categoria_soporte)
            )
        ).all()
        return {
            "total": int(total or 0),
            "terminadas": int(terminadas or 0),
            "tiempo_promedio_primera_respuesta_minutos": (
                round(float(promedio_respuesta), 2)
                if promedio_respuesta is not None
                else None
            ),
            "tiempo_promedio_resolucion_minutos": (
                round(float(promedio_resolucion), 2)
                if promedio_resolucion is not None
                else None
            ),
            "por_categoria": {
                categoria: cantidad
                for categoria, cantidad in por_categoria
            },
        }

    async def _diagnosticar_mikrotik(self, cliente: ClienteModel) -> dict:
        if not cliente.router:
            return {
                "disponible": False,
                "error": "Cliente sin MikroTik asignado",
            }

        def ejecutar():
            router = cliente.router
            mk = MikroTikService(
                router.ip_vpn,
                router.user_api,
                router.pass_api,
                router.port_api,
            )
            conectado, mensaje = mk.probar_conexion()
            if not conectado:
                return {
                    "disponible": False,
                    "error": f"MikroTik no disponible: {mensaje}",
                }
            sesion = (
                mk.obtener_info_sesion(cliente.user_pppoe)
                if cliente.user_pppoe
                else {"online": False}
            )
            trafico = (
                mk.obtener_consumo_interfaz_pppoe(cliente.user_pppoe)
                if cliente.user_pppoe and sesion.get("online")
                else {"up_bps": 0, "down_bps": 0}
            )
            ping = (
                mk.ping_desde_router(cliente.ip_asignada, count=3)
                if cliente.ip_asignada
                and cliente.ip_asignada != "0.0.0.0"
                else {"status": "sin_ip"}
            )
            return {
                "disponible": True,
                "pppoe_online": bool(sesion.get("online")),
                "ip_actual": sesion.get("ip"),
                "uptime": sesion.get("uptime"),
                "mac_reportada": sesion.get("mac_onu"),
                "ping_estado": ping.get("status"),
                "perdida_porcentaje": self.parsear_perdida(ping.get("loss")),
                "subida_bps": int(trafico.get("up_bps") or 0),
                "bajada_bps": int(trafico.get("down_bps") or 0),
            }

        try:
            return await asyncio.wait_for(
                asyncio.to_thread(ejecutar),
                timeout=35,
            )
        except asyncio.TimeoutError:
            return {
                "disponible": False,
                "error": "Tiempo de espera agotado consultando MikroTik",
            }
        except Exception as exc:
            return {
                "disponible": False,
                "error": f"Error consultando MikroTik: {exc}",
            }

    async def _diagnosticar_olt(self, cliente: ClienteModel) -> dict:
        if not cliente.olt or not cliente.onu_asignada:
            return {
                "disponible": False,
                "error": "Cliente sin OLT u ONU asignada",
            }

        olt = cliente.olt
        integracion = (olt.tipo_integracion or "snmp").strip().lower()
        errores = []
        if olt.api_enabled or integracion in {"vsol_api", "auto"}:
            try:
                datos = await asyncio.wait_for(
                    VsolApiService(self.db).monitorear_cliente_individual_api(
                        cliente.id
                    ),
                    timeout=35,
                )
                return self.normalizar_olt(datos, "vsol_api")
            except Exception as exc:
                errores.append(f"VSOL API: {exc}")

        if integracion in {"snmp", "auto"} or errores:
            try:
                datos = await asyncio.wait_for(
                    SNMPMonitorService(self.db).monitorear_cliente_individual(
                        cliente.id
                    ),
                    timeout=35,
                )
                return self.normalizar_olt(datos, "snmp")
            except Exception as exc:
                errores.append(f"SNMP: {exc}")

        return {
            "disponible": False,
            "error": "; ".join(errores) or "La OLT no tiene integración de lectura",
        }

    @classmethod
    def normalizar_olt(cls, datos: dict, origen: str) -> dict:
        estado = str(
            datos.get("estado_fisico")
            or datos.get("status")
            or ""
        ).strip().lower()
        return {
            "disponible": True,
            "onu_online": estado in {"online", "working", "up"},
            "potencia_rx_dbm": cls.parsear_decimal(
                datos.get("rx_power") or datos.get("potencia")
            ),
            "potencia_tx_dbm": cls.parsear_decimal(datos.get("tx_power")),
            "origen": origen,
            "estado_fisico": estado,
            "recomendacion_origen": datos.get("recomendacion"),
        }

    @staticmethod
    def clasificar(
        categoria: str,
        estado_cliente: str,
        mikrotik: dict,
        olt: dict,
    ) -> dict:
        if estado_cliente == "suspendido":
            return {
                "resultado": "advertencia",
                "codigo": "servicio_suspendido",
                "sugerencia": (
                    "El cliente está suspendido administrativamente. "
                    "Revisar adeudos antes de intervenir la red."
                ),
            }

        mk_ok = bool(mikrotik.get("disponible"))
        olt_ok = bool(olt.get("disponible"))
        pppoe = mikrotik.get("pppoe_online")
        onu = olt.get("onu_online")
        rx = SupportService.parsear_decimal(olt.get("potencia_rx_dbm"))
        ping = mikrotik.get("ping_estado")
        perdida = SupportService.parsear_decimal(
            mikrotik.get("perdida_porcentaje")
        )
        subida = int(mikrotik.get("subida_bps") or 0)
        bajada = int(mikrotik.get("bajada_bps") or 0)

        if not mk_ok and not olt_ok:
            return {
                "resultado": "incompleto",
                "codigo": "equipos_no_disponibles",
                "sugerencia": (
                    "No fue posible consultar MikroTik ni OLT. "
                    "Verificar conectividad y credenciales de gestión."
                ),
            }
        if olt_ok and onu is False:
            return {
                "resultado": "critico",
                "codigo": "onu_fuera_linea",
                "sugerencia": (
                    "La OLT reporta la ONU fuera de línea. Revisar energía, "
                    "patchcord, conectores y posible ruptura de fibra."
                ),
            }
        if rx is not None and (rx < Decimal("-27") or rx > Decimal("-8")):
            return {
                "resultado": "critico",
                "codigo": "potencia_optica_critica",
                "sugerencia": (
                    f"Potencia óptica crítica ({rx} dBm). Revisar conectores, "
                    "dobleces, empalmes y divisor óptico."
                ),
            }
        if rx is not None and rx < Decimal("-25"):
            return {
                "resultado": "advertencia",
                "codigo": "potencia_optica_baja",
                "sugerencia": (
                    f"Potencia óptica baja ({rx} dBm). Todavía hay enlace, "
                    "pero conviene limpiar conectores y revisar el margen óptico."
                ),
            }
        if categoria == "potencia_baja" and olt_ok and rx is None:
            return {
                "resultado": "incompleto",
                "codigo": "potencia_no_reportada",
                "sugerencia": (
                    "La OLT respondió, pero no entregó potencia óptica. "
                    "Validar compatibilidad, serial y lectura directa en la OLT."
                ),
            }
        if mk_ok and pppoe is False:
            return {
                "resultado": "critico",
                "codigo": "pppoe_sin_sesion",
                "sugerencia": (
                    "La ONU parece disponible, pero no existe sesión PPPoE. "
                    "Revisar credenciales, router del cliente y perfil PPPoE."
                ),
            }
        if mk_ok and pppoe and ping == "offline":
            return {
                "resultado": "advertencia",
                "codigo": "pppoe_sin_ping",
                "sugerencia": (
                    "Existe sesión PPPoE, pero el cliente no responde al ping. "
                    "Revisar CPE, firewall o dirección IP activa."
                ),
            }
        if perdida is not None and perdida >= Decimal("20"):
            return {
                "resultado": "advertencia",
                "codigo": "perdida_paquetes_alta",
                "sugerencia": (
                    f"Se detectó {perdida}% de pérdida de paquetes. Revisar "
                    "enlace, saturación, cableado y estabilidad del CPE."
                ),
            }
        if (
            categoria in {"sin_internet", "lentitud"}
            and mk_ok
            and pppoe
            and subida == 0
            and bajada == 0
        ):
            return {
                "resultado": "advertencia",
                "codigo": "conectado_sin_trafico",
                "sugerencia": (
                    "El cliente está conectado pero no presenta tráfico. "
                    "Validar por cable, señal Wi-Fi y consumo del dispositivo."
                ),
            }
        if categoria == "router_wifi" and pppoe and (onu is True or not olt_ok):
            return {
                "resultado": "advertencia",
                "codigo": "probable_wifi_local",
                "sugerencia": (
                    "La conectividad WAN parece correcta. Revisar alimentación, "
                    "canal, cobertura y configuración del router Wi-Fi."
                ),
            }
        if not mk_ok or not olt_ok:
            return {
                "resultado": "incompleto",
                "codigo": "diagnostico_parcial",
                "sugerencia": (
                    "El diagnóstico es parcial porque uno de los equipos de "
                    "gestión no respondió. Revisar el componente no disponible."
                ),
            }
        return {
            "resultado": "saludable",
            "codigo": "red_sin_falla_evidente",
            "sugerencia": (
                "PPPoE, ping y potencia no muestran una falla evidente. "
                "Realizar prueba local por cable y revisar el equipo del cliente."
            ),
        }

    @staticmethod
    def resumen_texto(diagnostico: DiagnosticoSoporteModel) -> str:
        rx = (
            f"{diagnostico.potencia_rx_dbm} dBm"
            if diagnostico.potencia_rx_dbm is not None
            else "N/D"
        )
        return (
            f"Resultado: {diagnostico.resultado}. "
            f"PPPoE: {SupportService.texto_estado(diagnostico.pppoe_online)}. "
            f"Ping: {diagnostico.ping_estado or 'N/D'}. "
            f"ONU: {SupportService.texto_estado(diagnostico.onu_online)}. "
            f"RX: {rx}. Sugerencia: {diagnostico.sugerencia}"
        )

    @staticmethod
    def texto_estado(valor) -> str:
        if valor is True:
            return "online"
        if valor is False:
            return "offline"
        return "N/D"

    @staticmethod
    def parsear_decimal(valor) -> Optional[Decimal]:
        if valor is None:
            return None
        try:
            texto = str(valor).lower().replace("dbm", "").strip()
            return Decimal(texto).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            return None

    @staticmethod
    def parsear_perdida(valor) -> Optional[Decimal]:
        if valor is None:
            return None
        return SupportService.parsear_decimal(str(valor).replace("%", ""))

    @staticmethod
    def normalizar_categoria(categoria: str) -> str:
        valor = (categoria or "").strip().lower()
        if valor not in CATEGORIAS_SOPORTE:
            raise ValueError("Categoría de soporte inválida")
        return valor

    @staticmethod
    def prioridad_sugerida(categoria: str) -> str:
        if categoria in {"sin_internet", "cable_roto"}:
            return "alta"
        if categoria in {"potencia_baja", "lentitud"}:
            return "normal"
        return "baja"
