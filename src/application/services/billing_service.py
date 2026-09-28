from datetime import date, datetime, timedelta
from decimal import Decimal
import logging
from types import SimpleNamespace
from typing import Optional, List
from dateutil.relativedelta import relativedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_, desc, func, extract
from sqlalchemy.orm import joinedload, selectinload

# Modelos
from src.infrastructure.models import (
    ClienteModel, FacturaModel, FacturaConceptoModel, PagoConceptoModel, PagoModel,
    UsuarioModel, PlanModel, RouterModel, PlantillaFacturacionModel,
    ServicioAdicionalModel,
    ServicioModel,
    SuspensionFacturacionModel,
    ConfiguracionSistema,
    LogCronjobModel,
)

# Servicios e Helpers
from src.infrastructure.mikrotik_service import MikroTikService
from src.utils.mikrotik import formatear_rate_limit_dhcp, normalizar_mac
# 👇 IMPORTAMOS EL NUEVO SERVICIO UNIFICADO 👇
from src.application.services.notification_service import NotificationService
from src.application.helpers.pdf_generator import generar_recibo_pdf

from src.infrastructure.models import (
    ServicioModel,
    TipoFacturacion,
    CicloFacturacion,
)

from src.application.services.billing_calendar_service import (
    BillingCalendarService,
)
from src.application.services.finance_service import FinanceService


logger = logging.getLogger(__name__)


MESES_EN_ESPANOL = (
    "Enero",
    "Febrero",
    "Marzo",
    "Abril",
    "Mayo",
    "Junio",
    "Julio",
    "Agosto",
    "Septiembre",
    "Octubre",
    "Noviembre",
    "Diciembre",
)
CONCEPTO_RECONEXION_PROMESA = (
    "Reconexión y penalización por incumplir promesa de pago"
)


class BillingService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def crear_prorrateo_inicial(
        self,
        servicio: ServicioModel,
        cliente: ClienteModel,
        plan: PlanModel,
        plantilla: PlantillaFacturacionModel,
    ) -> FacturaModel | None:
        """Crea el prorrateo pagable sin permitir que origine un corte."""
        if (
            not servicio.proxima_facturacion
            or not plan
            or not plantilla
            or servicio.ciclo_facturacion == CicloFacturacion.aniversario
        ):
            return None
        periodo = BillingCalendarService.calcular_periodo_por_dia_ciclo(
            servicio.proxima_facturacion,
            plantilla.dia_pago or servicio.dia_vencimiento or 1,
            plan.precio,
            plantilla.impuesto or 0,
        )
        if not periodo.es_prorrateada:
            return None
        existente = (
            await self.db.execute(
                select(FacturaModel).where(
                    FacturaModel.servicio_id == servicio.id,
                    FacturaModel.periodo_desde == periodo.periodo_desde,
                    FacturaModel.periodo_hasta == periodo.periodo_hasta,
                )
            )
        ).scalars().first()
        if existente:
            return existente

        tipo_snapshot = getattr(
            servicio.tipo_facturacion, "value", servicio.tipo_facturacion
        )
        ciclo_snapshot = getattr(
            servicio.ciclo_facturacion, "value", servicio.ciclo_facturacion
        )
        descripcion = BillingCalendarService.describir_dias_cobrados(
            periodo.periodo_desde, periodo.periodo_hasta
        )
        factura = FacturaModel(
            cliente_id=cliente.id,
            servicio_id=servicio.id,
            plan_snapshot=plan.nombre,
            tipo_factura="prorrateo",
            concepto="Prorrateo inicial de internet",
            detalles=f"Prorrateo Internet - {plan.nombre}",
            descripcion=descripcion,
            monto=periodo.subtotal,
            impuesto=periodo.impuesto,
            total=periodo.total,
            saldo_pendiente=periodo.total,
            estado="pendiente",
            fecha_emision=date.today(),
            fecha_vencimiento=periodo.siguiente_facturacion,
            fecha_limite_corte=(
                periodo.siguiente_facturacion
                + timedelta(days=plantilla.dias_tolerancia or 0)
            ),
            mes_correspondiente=(
                f"Prorrateo {periodo.periodo_desde.strftime('%d/%m/%Y')} - "
                f"{periodo.periodo_hasta.strftime('%d/%m/%Y')}"
            ),
            periodo_desde=periodo.periodo_desde,
            periodo_hasta=periodo.periodo_hasta,
            dias_facturados=periodo.dias_facturados,
            dias_periodo=periodo.dias_periodo,
            precio_mensual_snapshot=periodo.precio_mensual,
            precio_diario=periodo.precio_diario,
            es_prorrateada=True,
            tipo_facturacion_snapshot=str(tipo_snapshot),
            ciclo_facturacion_snapshot=str(ciclo_snapshot),
            monto_servicio_original=periodo.subtotal,
            impuesto_servicio_original=periodo.impuesto,
            cargos_adicionales_total=0,
            dias_con_servicio=periodo.dias_facturados,
            dias_sin_servicio=0,
            ajuste_suspension=0,
            afecta_corte=False,
        )
        self.db.add(factura)
        await self.db.flush()
        self.db.add(FacturaConceptoModel(
            factura_id=factura.id,
            cliente_id=cliente.id,
            servicio_id=servicio.id,
            tipo="internet_prorrateado",
            concepto="Prorrateo de internet",
            descripcion=descripcion,
            monto_original=periodo.total,
            saldo_pendiente=periodo.total,
            estado="facturado",
            afecta_corte=False,
            fecha_cargo=periodo.periodo_desde,
        ))
        servicio.proxima_facturacion = periodo.siguiente_facturacion
        cliente.proxima_factura = periodo.siguiente_facturacion
        return factura

    async def _agregar_reconexion_a_factura(
        self,
        factura: FacturaModel,
        cliente: ClienteModel,
        servicio: ServicioModel | None,
        por_promesa_incumplida: bool = False,
    ) -> Decimal:
        """Suma el cargo de reconexión de la plantilla a la factura del corte.

        Se agrega al cortar, una sola vez por corte, para que el cliente lo
        pague en el mismo cobro que lo reconecta. Cuenta para el corte: sin
        pagarlo no se reconecta. Con cargo 0 en la plantilla no se agrega.
        Si el corte es por una promesa de pago incumplida, el renglón lo dice
        para que el cliente entienda por qué paga otra reconexión.
        """
        plantilla_id = (
            servicio.plantilla_id if servicio and servicio.plantilla_id
            else cliente.plantilla_id
        )
        plantilla = (
            await self.db.get(PlantillaFacturacionModel, plantilla_id)
            if plantilla_id else None
        )
        monto = (
            Decimal(str(plantilla.cargo_reconexion or 0))
            if plantilla else Decimal("0")
        ).quantize(Decimal("0.01"))
        if monto <= 0:
            return Decimal("0.00")

        concepto = (
            CONCEPTO_RECONEXION_PROMESA if por_promesa_incumplida
            else "Cargo por reconexión"
        )
        self.db.add(FacturaConceptoModel(
            cliente_id=cliente.id,
            servicio_id=servicio.id if servicio else None,
            factura_id=factura.id,
            tipo="reconexion",
            concepto=concepto,
            descripcion=(
                "Reconexión por corte al incumplir la promesa de pago"
                if por_promesa_incumplida
                else "Reconexión por corte de servicio"
            ),
            monto_original=monto,
            saldo_pendiente=monto,
            estado="facturado",
            afecta_corte=True,
            fecha_cargo=date.today(),
        ))
        factura.monto = Decimal(factura.monto or 0) + monto
        factura.total = Decimal(factura.total or 0) + monto
        factura.saldo_pendiente = Decimal(factura.saldo_pendiente or 0) + monto
        factura.cargos_adicionales_total = (
            Decimal(factura.cargos_adicionales_total or 0) + monto
        )
        factura.detalles = "\n".join(
            [factura.detalles or "", f"{concepto}: ${monto:.2f}"]
        ).strip()
        return monto

    async def _consolidar_prorrateos_en_mensualidad(
        self,
        factura: FacturaModel,
        servicio: ServicioModel,
    ) -> int:
        """Traslada prorrateos abiertos a la primera mensualidad normal."""
        if factura.es_prorrateada:
            return 0

        prorrateos = (
            await self.db.execute(
                select(FacturaModel)
                .where(
                    FacturaModel.servicio_id == servicio.id,
                    FacturaModel.id != factura.id,
                    FacturaModel.tipo_factura == "prorrateo",
                    FacturaModel.estado.in_(["pendiente", "vencida"]),
                    FacturaModel.saldo_pendiente > 0,
                    FacturaModel.periodo_hasta < factura.periodo_desde,
                )
                .order_by(FacturaModel.periodo_desde.asc())
                .with_for_update()
            )
        ).scalars().all()

        total_consolidado = Decimal("0")
        for prorrateo in prorrateos:
            saldo = Decimal(prorrateo.saldo_pendiente or 0)
            if saldo <= 0:
                continue
            total_consolidado += saldo
            factura.monto = Decimal(factura.monto or 0) + saldo
            factura.total = Decimal(factura.total or 0) + saldo
            factura.saldo_pendiente = Decimal(factura.saldo_pendiente or 0) + saldo
            factura.monto_servicio_original = (
                Decimal(factura.monto_servicio_original or 0)
                + saldo
            )
            self.db.add(FacturaConceptoModel(
                factura_id=factura.id,
                cliente_id=factura.cliente_id,
                servicio_id=servicio.id,
                tipo="internet_prorrateado",
                concepto="Prorrateo de internet",
                descripcion=prorrateo.descripcion or prorrateo.detalles,
                monto_original=saldo,
                saldo_pendiente=saldo,
                estado="facturado",
                afecta_corte=True,
                fecha_cargo=prorrateo.periodo_desde,
            ))
            conceptos_anteriores = (
                await self.db.execute(
                    select(FacturaConceptoModel).where(
                        FacturaConceptoModel.factura_id == prorrateo.id,
                        FacturaConceptoModel.saldo_pendiente > 0,
                    )
                )
            ).scalars().all()
            for concepto in conceptos_anteriores:
                concepto.saldo_pendiente = 0
                concepto.estado = "consolidado"
            prorrateo.saldo_pendiente = 0
            prorrateo.estado = "consolidada"
            prorrateo.afecta_corte = False
            nota = f"Consolidada en factura #{factura.id}."
            prorrateo.descripcion = " ".join(
                parte for parte in [prorrateo.descripcion, nota] if parte
            )

        if total_consolidado > 0:
            detalle = f"Prorrateo anterior consolidado: ${total_consolidado:.2f}"
            factura.detalles = "\n".join(
                parte for parte in [factura.detalles, detalle] if parte
            )
        return len(prorrateos)

    @staticmethod
    def _variables_detalle_factura(factura):
        """Variables comunes para recibos y plantillas de WhatsApp."""
        def fecha(valor):
            return valor.strftime("%d/%m/%Y") if valor else "N/A"

        def dinero(valor):
            return f"${float(valor or 0):.2f}"

        return {
            "detalle_cobro": (
                factura.detalles
                or factura.descripcion
                or "Detalle no disponible"
            ),
            "periodo_desde": fecha(factura.periodo_desde),
            "periodo_hasta": fecha(factura.periodo_hasta),
            "dias_con_servicio": str(factura.dias_con_servicio or 0),
            "dias_sin_servicio": str(factura.dias_sin_servicio or 0),
            "monto_servicio_original": dinero(
                factura.monto_servicio_original
            ),
            "ajuste_suspension": dinero(factura.ajuste_suspension),
            "cargos_adicionales": dinero(
                factura.cargos_adicionales_total
            ),
            "total_factura": dinero(factura.total),
        }

    # ==========================================
    # 1. GENERACIÓN MASIVA INTELIGENTE
    # ==========================================
    async def generar_emision_masiva(self, dia_objetivo: int = None):
        hoy = date.today()
        notificador = NotificationService(self.db)

        stmt = (
            select(ServicioModel)
            .options(
                selectinload(ServicioModel.cliente),
                selectinload(ServicioModel.cliente).selectinload(
                    ClienteModel.plantilla
                ),
                selectinload(ServicioModel.cliente).selectinload(
                    ClienteModel.plan
                ),
                selectinload(ServicioModel.plantilla),
                selectinload(ServicioModel.plan),
            )
            .join(ClienteModel, ClienteModel.id == ServicioModel.cliente_id)
            .where(
                ServicioModel.estado.in_(["activo", "suspendido"]),
                or_(
                    ServicioModel.plantilla_id.isnot(None),
                    ClienteModel.plantilla_id.isnot(None),
                ),
                or_(
                    ServicioModel.plan_id.isnot(None),
                    ClienteModel.plan_id.isnot(None),
                ),
            )
        )

        result = await self.db.execute(stmt)
        servicios = result.scalars().all()

        reporte = {
            "total_procesados": 0,
            "facturas_generadas": 0,
            "facturas_prorrateadas": 0,
            "facturas_prepago": 0,
            "facturas_postpago": 0,
            "mensajes_enviados": 0,
            "omitidos_sin_servicio": 0,
            "omitidos_sin_plantilla": 0,
            "omitidos_sin_proxima_facturacion": 0,
            "omitidos_modalidad_pendiente": 0,
            "omitidos_no_toca_emitir": 0,
            "omitidos_factura_existente": 0,
            "omitidos_periodo_anulado": 0,
        }

        for servicio in servicios:
            reporte["total_procesados"] += 1
            cliente = servicio.cliente
            plantilla = servicio.plantilla or getattr(cliente, "plantilla", None)
            if plantilla is None:
                reporte["omitidos_sin_plantilla"] += 1
                continue
            plan = servicio.plan or getattr(cliente, "plan", None)
            if plan is None:
                reporte["omitidos_sin_servicio"] += 1
                continue
            if dia_objetivo and plantilla.dia_pago != dia_objetivo:
                continue
            if servicio.ciclo_facturacion == CicloFacturacion.aniversario:
                reporte["omitidos_modalidad_pendiente"] += 1
                continue
            if servicio.proxima_facturacion is None:
                reporte["omitidos_sin_proxima_facturacion"] += 1
                continue

            if servicio.estado == "suspendido":
                await FinanceService(
                    self.db
                ).normalizar_facturas_suspendidas(
                    servicio,
                    solo_periodos_cerrados=True,
                )

            tipo_snapshot = servicio.tipo_facturacion.value if hasattr(servicio.tipo_facturacion, "value") else str(servicio.tipo_facturacion)
            ciclo_snapshot = servicio.ciclo_facturacion.value if hasattr(servicio.ciclo_facturacion, "value") else str(servicio.ciclo_facturacion)

            if tipo_snapshot not in {"prepago", "postpago"}:
                reporte["omitidos_modalidad_pendiente"] += 1
                continue

            # La plantilla es la configuración vigente del ciclo. El campo
            # del servicio sólo queda como dato histórico/compatibilidad.
            dia_ciclo = plantilla.dia_pago or servicio.dia_vencimiento or 1
            periodo = BillingCalendarService.calcular_periodo_por_dia_ciclo(
                periodo_desde=servicio.proxima_facturacion,
                dia_ciclo=dia_ciclo,
                precio_mensual=plan.precio,
                impuesto_porcentaje=plantilla.impuesto or 0,
            )
            fecha_vencimiento = BillingCalendarService.calcular_fecha_vencimiento(periodo, tipo_snapshot)
            fecha_generacion = BillingCalendarService.calcular_fecha_generacion(
                periodo,
                tipo_snapshot,
                plantilla.dias_antes_emision or 0,
            )

            # Incluso en ejecución manual no se permiten periodos futuros.
            # El modo manual sirve para recuperar una emisión atrasada, no
            # para adelantar varios meses al ejecutar el endpoint repetidas
            # veces.
            if hoy < fecha_generacion:
                reporte["omitidos_no_toca_emitir"] += 1
                continue

            stmt_dup = select(FacturaModel).where(
                and_(
                    FacturaModel.servicio_id == servicio.id,
                    FacturaModel.periodo_desde == periodo.periodo_desde,
                    FacturaModel.periodo_hasta == periodo.periodo_hasta,
                )
            )
            factura_existente = (
                await self.db.execute(stmt_dup)
            ).scalars().first()
            if factura_existente:
                if factura_existente.estado == "anulada":
                    # Una anulación conserva el comprobante original. Para
                    # reemitir se debe indicar una nueva fecha de facturación,
                    # produciendo un periodo distinto y auditable.
                    reporte["omitidos_periodo_anulado"] += 1
                    continue
                servicio.proxima_facturacion = periodo.siguiente_facturacion
                reporte["omitidos_factura_existente"] += 1
                continue

            if periodo.es_prorrateada:
                mes_actual_str = f"Prorrateo {periodo.periodo_desde.strftime('%d/%m/%Y')} - {periodo.periodo_hasta.strftime('%d/%m/%Y')}"
                detalles = (
                    f"Prorrateo Internet - {plan.nombre} "
                    f"({servicio.alias}: {servicio.direccion or 'Sin dirección'}) "
                    f"({periodo.dias_facturados} de "
                    f"{periodo.dias_periodo} días)"
                )
            else:
                if periodo.periodo_desde.day == 1:
                    mes_actual_str = (
                        f"{MESES_EN_ESPANOL[periodo.periodo_desde.month - 1]} "
                        f"{periodo.periodo_desde.year}"
                    )
                else:
                    mes_actual_str = f"Ciclo {periodo.periodo_desde.strftime('%d/%m/%Y')} - {periodo.periodo_hasta.strftime('%d/%m/%Y')}"
                detalles = (
                    f"Servicio Internet - {plan.nombre} "
                    f"({servicio.alias}: "
                    f"{servicio.direccion or 'Sin dirección'})"
                )

            nueva_factura = FacturaModel(
                cliente_id=cliente.id,
                servicio_id=servicio.id,
                plan_snapshot=plan.nombre,
                tipo_factura=("prorrateo" if periodo.es_prorrateada else "mensual"),
                concepto="Servicio de internet",
                detalles=detalles,
                descripcion=BillingCalendarService.describir_dias_cobrados(
                    periodo.periodo_desde,
                    periodo.periodo_hasta,
                ),
                monto=periodo.subtotal,
                impuesto=periodo.impuesto,
                total=periodo.total,
                saldo_pendiente=periodo.total,
                estado="pendiente",
                fecha_emision=hoy,
                fecha_vencimiento=fecha_vencimiento,
                fecha_limite_corte=fecha_vencimiento + timedelta(days=plantilla.dias_tolerancia or 0),
                mes_correspondiente=mes_actual_str,
                periodo_desde=periodo.periodo_desde,
                periodo_hasta=periodo.periodo_hasta,
                dias_facturados=periodo.dias_facturados,
                dias_periodo=periodo.dias_periodo,
                precio_mensual_snapshot=periodo.precio_mensual,
                precio_diario=periodo.precio_diario,
                es_prorrateada=periodo.es_prorrateada,
                tipo_facturacion_snapshot=tipo_snapshot,
                ciclo_facturacion_snapshot=ciclo_snapshot,
                monto_servicio_original=periodo.subtotal,
                impuesto_servicio_original=periodo.impuesto,
                cargos_adicionales_total=0,
                dias_con_servicio=periodo.dias_facturados,
                dias_sin_servicio=0,
                ajuste_suspension=0,
                afecta_corte=not periodo.es_prorrateada,
            )

            self.db.add(nueva_factura)
            await self.db.flush()

            if (
                servicio.estado == "suspendido"
                and periodo.periodo_hasta < hoy
            ):
                await FinanceService(
                    self.db
                ).recalcular_factura_por_suspension(
                    nueva_factura,
                    servicio,
                )

            concepto_internet = FacturaConceptoModel(
                factura_id=nueva_factura.id,
                cliente_id=cliente.id,
                servicio_id=servicio.id,
                tipo="internet",
                concepto=(
                    "Internet prorrateado"
                    if periodo.es_prorrateada
                    else "Servicio de internet"
                ),
                descripcion=nueva_factura.descripcion,
                monto_original=nueva_factura.total,
                saldo_pendiente=nueva_factura.saldo_pendiente,
                estado="facturado",
                afecta_corte=not periodo.es_prorrateada,
                fecha_cargo=periodo.periodo_desde,
            )
            self.db.add(concepto_internet)

            await self._consolidar_prorrateos_en_mensualidad(
                nueva_factura,
                servicio,
            )

            if not periodo.es_prorrateada:
                adicionales = (
                    await self.db.execute(
                        select(ServicioAdicionalModel).where(
                            ServicioAdicionalModel.cliente_id == cliente.id,
                            ServicioAdicionalModel.activo.is_(True),
                            ServicioAdicionalModel.fecha_inicio <= periodo.periodo_hasta,
                            or_(
                                ServicioAdicionalModel.servicio_id == servicio.id,
                                ServicioAdicionalModel.servicio_id.is_(None),
                            ),
                        )
                    )
                ).scalars().all()
                for adicional in adicionales:
                    precio_adicional = Decimal(adicional.precio_mensual or 0)
                    if precio_adicional <= 0:
                        continue
                    nueva_factura.monto = Decimal(nueva_factura.monto or 0) + precio_adicional
                    nueva_factura.total = Decimal(nueva_factura.total or 0) + precio_adicional
                    nueva_factura.saldo_pendiente = Decimal(nueva_factura.saldo_pendiente or 0) + precio_adicional
                    nueva_factura.cargos_adicionales_total = Decimal(nueva_factura.cargos_adicionales_total or 0) + precio_adicional
                    self.db.add(FacturaConceptoModel(
                        factura_id=nueva_factura.id,
                        cliente_id=cliente.id,
                        servicio_id=servicio.id,
                        tipo="servicio_adicional",
                        concepto=adicional.nombre,
                        descripcion=f"Servicio adicional mensual: {adicional.nombre}",
                        monto_original=precio_adicional,
                        saldo_pendiente=precio_adicional,
                        estado="facturado",
                        afecta_corte=adicional.afecta_corte,
                        fecha_cargo=periodo.periodo_desde,
                    ))
                    if adicional.periodicidad == "unico":
                        adicional.activo = False

            cargos_pendientes = (
                await self.db.execute(
                    select(FacturaConceptoModel)
                    .where(
                        FacturaConceptoModel.cliente_id == cliente.id,
                        FacturaConceptoModel.factura_id.is_(None),
                        FacturaConceptoModel.estado == "pendiente",
                        FacturaConceptoModel.fecha_cargo <= hoy,
                        or_(
                            FacturaConceptoModel.servicio_id == servicio.id,
                            FacturaConceptoModel.servicio_id.is_(None),
                        ),
                    )
                    .order_by(
                        FacturaConceptoModel.fecha_cargo.asc(),
                        FacturaConceptoModel.id.asc(),
                    )
                    .with_for_update()
                )
            ).scalars().all()
            total_cargos = sum(
                (Decimal(cargo.saldo_pendiente or 0) for cargo in cargos_pendientes),
                Decimal("0.00"),
            )
            if total_cargos:
                nueva_factura.monto = Decimal(nueva_factura.monto or 0) + total_cargos
                nueva_factura.total = Decimal(nueva_factura.total or 0) + total_cargos
                nueva_factura.saldo_pendiente = Decimal(nueva_factura.saldo_pendiente or 0) + total_cargos
                nueva_factura.cargos_adicionales_total = total_cargos
                for cargo in cargos_pendientes:
                    cargo.factura_id = nueva_factura.id
                    cargo.estado = "facturado"
                detalle_cargos = [
                    f"{cargo.concepto}"
                    + (
                        f" (cuota {cargo.numero_cuota}/{cargo.total_cuotas})"
                        if cargo.numero_cuota and cargo.total_cuotas
                        else ""
                    )
                    + f": ${Decimal(cargo.saldo_pendiente or 0):.2f}"
                    for cargo in cargos_pendientes
                ]
                nueva_factura.detalles = "\n".join(
                    [nueva_factura.detalles or "", *detalle_cargos]
                ).strip()
                await self.db.flush()

            # El crédito se aplica después de cualquier prorrateo para no
            # consumirlo contra un importe provisional. Una factura del ciclo
            # abierto de un servicio suspendido se ajustará al reactivarlo.
            if (
                servicio.estado != "suspendido"
                or periodo.periodo_hasta < hoy
            ):
                pago_credito = await FinanceService(self.db).aplicar_saldo_favor_automatico(
                    nueva_factura,
                    cliente,
                )
                if pago_credito:
                    await self._distribuir_pago_en_conceptos(
                        nueva_factura,
                        pago_credito,
                        None,
                    )

            servicio.proxima_facturacion = periodo.siguiente_facturacion
            reporte["facturas_generadas"] += 1
            if periodo.es_prorrateada:
                reporte["facturas_prorrateadas"] += 1
            if tipo_snapshot == "postpago":
                reporte["facturas_postpago"] += 1
            else:
                reporte["facturas_prepago"] += 1

            debe_notificar = (
                nueva_factura.saldo_pendiente > 0
                and not periodo.es_prorrateada
                and (
                    servicio.estado != "suspendido"
                    or periodo.periodo_hasta < hoy
                )
            )
            if cliente.telefono and debe_notificar:
                await self.db.flush()
                estado_cuenta = await self.estado_cuenta_cliente(cliente.id)
                exito = await notificador.notificar(
                    "nueva_factura",
                    cliente.id,
                    variables_extra={
                        **self._variables_detalle_factura(nueva_factura),
                        **self._variables_total_a_pagar(estado_cuenta),
                        "monto": f"${nueva_factura.total}",
                        "folio": str(nueva_factura.id),
                        "mes_actual": mes_actual_str,
                    },
                    clave_dedupe=f"factura:{nueva_factura.id}:emitida",
                )
                if exito:
                    reporte["mensajes_enviados"] += 1

        await self.db.commit()
        return reporte



    

    # ==========================================
    # 4. MOTOR DE RECORDATORIOS (CON INTERRUPTOR 0 = DESACTIVADO)
    # ==========================================


    async def enviar_recordatorios_automaticos(self, dias_aviso_urgente: int = 1):
        # 🔥 EL INTERRUPTOR INTELIGENTE: Si se configura en 0, la opción queda DESACTIVADA 🔥
        if dias_aviso_urgente <= 0:
            return {"status": "desactivado", "aviso_urgente_enviados": 0}
            
        hoy = date.today()
        notificador = NotificationService(self.db)
        
        # Buscamos todas las facturas pendientes que no tengan promesa activa
        stmt = select(FacturaModel).options(
            joinedload(FacturaModel.cliente)
        ).where(
            FacturaModel.estado == 'pendiente',
            FacturaModel.es_promesa_activa == False
        )
        
        facturas = (await self.db.execute(stmt)).scalars().all()
        reporte = {"status": "activo", "aviso_urgente_enviados": 0}

        for factura in facturas:
            cliente = factura.cliente
            # Solo enviamos a clientes activos con teléfono registrado
            if not cliente.telefono or cliente.estado != 'activo':
                continue

            # Calculamos cuántos días faltan para la fecha de pago
            dias_restantes = (factura.fecha_vencimiento - hoy).days
            
            # Se ejecuta dinámicamente según los días del parámetro (ej: 1 día antes)
            if dias_restantes == dias_aviso_urgente:
                try:
                    # 🔥 CORRECCIÓN: Llamamos a la nueva plantilla 'recordatorio_pago'
                    # Ya no mandamos variables_extra porque el Notificador Global
                    # se encarga de inyectar el {precio}, {dia_corte}, etc.
                    estado_cuenta = await self.estado_cuenta_cliente(cliente.id)
                    await notificador.notificar(
                        tipo_evento="recordatorio_pago", 
                        cliente_id=cliente.id,
                        variables_extra=self._variables_total_a_pagar(
                            estado_cuenta
                        ),
                        clave_dedupe=(
                            f"factura:{factura.id}:recordatorio:"
                            f"{hoy.isoformat()}:{dias_aviso_urgente}"
                        ),
                    )
                    reporte["aviso_urgente_enviados"] += 1
                except Exception as e:
                    print(f"⚠️ Error enviando aviso previo a {cliente.nombre}: {e}")

        await self.db.commit()
        return reporte

    # ==========================================
    # 2. MOTOR DE CORTES AUTOMÁTICOS (CORREGIDO)
    # ==========================================

    async def procesar_cortes_automaticos(self):
        # Corta morosos y sincroniza factura, cliente y servicio.
        hoy = date.today()
        notificador = NotificationService(self.db)

        stmt = (
            select(FacturaModel)
            .join(ClienteModel, ClienteModel.id == FacturaModel.cliente_id)
            .options(
                joinedload(FacturaModel.cliente).joinedload(
                    ClienteModel.router
                ),
                joinedload(FacturaModel.servicio).joinedload(
                    ServicioModel.router
                ),
            )
            .where(
                *self._condiciones_deuda_cortable(hoy),
                ClienteModel.estado != "eliminado",
            )
        )

        facturas = (
            await self.db.execute(stmt)
        ).unique().scalars().all()

        reporte = {
            "clientes_suspendidos": 0,
            "servicios_suspendidos": 0,
            "facturas_vencidas": 0,
            "promesas_rotas": 0,
            "notificaciones_enviadas": 0,
            "errores_mikrotik": 0,
            "errores_notificacion": 0,
            "omitidos_ya_suspendidos": 0,
        }
        clientes_suspendidos = set()

        for factura in facturas:
            cliente = factura.cliente
            servicio = factura.servicio
            objetivo = servicio or cliente
            estado_previo = objetivo.estado
            promesa_rota = bool(factura.es_promesa_activa)
            if promesa_rota:
                factura.es_promesa_activa = False
                await FinanceService(self.db).marcar_promesa_incumplida(
                    factura.id
                )
                reporte["promesas_rotas"] += 1

            if not objetivo.router or not objetivo.ip_asignada:
                reporte["errores_mikrotik"] += 1
                continue

            try:
                mk = MikroTikService(
                    objetivo.router.ip_vpn,
                    objetivo.router.user_api,
                    objetivo.router.pass_api,
                    objetivo.router.port_api,
                )
                resultado = mk.gestionar_corte_cliente(
                    objetivo.ip_asignada,
                    suspender=True,
                )

                if resultado is not True:
                    raise RuntimeError(
                        "MikroTik no confirmó la suspensión."
                    )

                if objetivo.user_pppoe:
                    mk.desconectar_cliente_activo(
                        objetivo.user_pppoe
                    )
            except Exception as exc:
                reporte["errores_mikrotik"] += 1
                print(
                    "⚠️ Error al cortar al cliente "
                    f"{cliente.id}: {exc}"
                )
                continue

            actualizados = (
                await self._actualizar_estado_servicio_factura(
                    factura,
                    "suspendido",
                )
            )
            if servicio:
                await self._abrir_suspension_facturacion(
                    servicio,
                    factura,
                    "promesa_incumplida" if promesa_rota else "falta_pago",
                )
            if estado_previo != "suspendido":
                await self._agregar_reconexion_a_factura(
                    factura,
                    cliente,
                    servicio,
                    por_promesa_incumplida=promesa_rota,
                )
            await self._sincronizar_estado_cliente(cliente.id)
            factura.estado = "vencida"

            clientes_suspendidos.add(cliente.id)
            reporte["servicios_suspendidos"] += actualizados
            reporte["facturas_vencidas"] += 1

            if estado_previo == "suspendido":
                # Reconciliar también los clientes que la BD ya marcaba
                # suspendidos repara entradas faltantes en el address-list.
                reporte["omitidos_ya_suspendidos"] += 1
                continue

            try:
                variables = {
                    "saldo_pendiente": (
                        f"${float(factura.saldo_pendiente or 0):.2f}"
                    ),
                    "fecha_corte": (
                        factura.fecha_limite_corte.strftime("%d/%m/%Y")
                        if factura.fecha_limite_corte
                        else "N/A"
                    ),
                    "folio": str(factura.id),
                }
                await self.db.flush()
                variables.update(
                    self._variables_total_a_pagar(
                        await self.estado_cuenta_cliente(cliente.id)
                    )
                )

                enviado = await notificador.notificar(
                    "corte_ejecutado",
                    cliente.id,
                    variables_extra=variables,
                    clave_dedupe=(
                        f"factura:{factura.id}:corte:"
                        f"{hoy.isoformat()}:ejecutado"
                    ),
                )

                if not enviado:
                    enviado = await notificador.notificar(
                        "aviso_corte",
                        cliente.id,
                        variables_extra=variables,
                        clave_dedupe=(
                            f"factura:{factura.id}:corte:"
                            f"{hoy.isoformat()}:aviso"
                        ),
                    )

                if enviado:
                    reporte["notificaciones_enviadas"] += 1
            except Exception as exc:
                reporte["errores_notificacion"] += 1
                print(
                    "⚠️ Corte confirmado, pero falló WhatsApp "
                    f"para el cliente {cliente.id}: {exc}"
                )

        reporte["clientes_suspendidos"] = len(clientes_suspendidos)
        await self.db.commit()
        return reporte

    async def reactivar_servicios_sin_deuda(self) -> dict[str, int]:
        """Reconecta servicios cortados por pago que ya no deben nada vencido.

        Repara pagos cuya reconexión falló en MikroTik (router caído o
        timeout) y servicios que quedaron suspendidos con la regla anterior.
        Sólo toca suspensiones abiertas por el motor de cobranza; las
        suspensiones manuales se respetan.
        """
        hoy = date.today()
        servicios = (
            await self.db.execute(
                select(ServicioModel)
                .join(
                    SuspensionFacturacionModel,
                    SuspensionFacturacionModel.servicio_id == ServicioModel.id,
                )
                .where(
                    ServicioModel.estado == "suspendido",
                    SuspensionFacturacionModel.fecha_fin.is_(None),
                    SuspensionFacturacionModel.motivo_inicio.in_(
                        ["falta_pago", "promesa_incumplida"]
                    ),
                )
                .order_by(ServicioModel.id)
            )
        ).unique().scalars().all()

        reporte = {"revisados": len(servicios), "reactivados": 0, "errores": 0}
        for servicio in servicios:
            referencia = SimpleNamespace(
                servicio_id=servicio.id,
                cliente_id=servicio.cliente_id,
            )
            if await self._servicio_tiene_deuda_pendiente(referencia):
                continue

            if not await self._reactivar_en_mikrotik(servicio):
                reporte["errores"] += 1
                self.db.add(LogCronjobModel(
                    nivel="ERROR",
                    origen="ReactivacionPagados",
                    mensaje=(
                        f"Servicio {servicio.id} sin deuda vencida sigue "
                        "suspendido: MikroTik no confirmó la reconexión; "
                        "se reintentará."
                    ),
                ))
                await self.db.commit()
                continue

            cliente = await self.db.get(ClienteModel, servicio.cliente_id)
            await self._cerrar_suspension_facturacion(servicio, hoy, "pago")
            servicio.estado = "activo"
            servicio.ultima_reactivacion_origen = "automatico"
            servicio.ultima_reactivacion_en = datetime.now()
            await self._sincronizar_estado_cliente(cliente.id)
            # El cargo de reconexión ya venía en la factura del corte.
            self.db.add(LogCronjobModel(
                nivel="WARNING",
                origen="ReactivacionPagados",
                mensaje=(
                    f"Servicio {servicio.id} ({cliente.nombre}) reactivado: "
                    "no tiene deuda vencida."
                ),
            ))
            await self.db.commit()
            reporte["reactivados"] += 1

            if cliente.telefono:
                try:
                    await NotificationService(self.db).notificar(
                        tipo_evento="reconexion",
                        cliente_id=cliente.id,
                        clave_dedupe=(
                            f"servicio:{servicio.id}:reconexion:"
                            f"{hoy.isoformat()}"
                        ),
                    )
                except Exception as exc:
                    print(
                        "⚠️ Reconexión confirmada, pero falló WhatsApp "
                        f"para el cliente {cliente.id}: {exc}"
                    )
        return reporte

    # ==========================================
    # BAJA AUTOMÁTICA POR FALTA DE PAGO
    # ==========================================
    async def dar_baja_por_falta_de_pago(self, dias: int) -> dict[str, int]:
        """Da de baja servicios con más de `dias` suspendidos por adeudo.

        Antes de la baja se cierran como no cobrados los días posteriores al
        corte: solo queda como deuda lo que el cliente usó.
        """
        from src.application.services.baja_service import BajaService

        reporte = {"revisados": 0, "dados_de_baja": 0, "errores": 0}
        if not dias or dias <= 0:
            return reporte
        limite = date.today() - timedelta(days=dias)
        servicios = (
            await self.db.execute(
                select(ServicioModel)
                .join(
                    SuspensionFacturacionModel,
                    SuspensionFacturacionModel.servicio_id == ServicioModel.id,
                )
                .where(
                    ServicioModel.estado == "suspendido",
                    SuspensionFacturacionModel.fecha_fin.is_(None),
                    SuspensionFacturacionModel.motivo_inicio.in_(
                        ["falta_pago", "promesa_incumplida"]
                    ),
                    SuspensionFacturacionModel.fecha_inicio <= limite,
                )
                .order_by(ServicioModel.id)
            )
        ).unique().scalars().all()
        reporte["revisados"] = len(servicios)
        if not servicios:
            return reporte

        operador = (
            await self.db.execute(
                select(UsuarioModel)
                .where(
                    UsuarioModel.rol == "admin",
                    UsuarioModel.activo.is_(True),
                )
                .order_by(UsuarioModel.id.asc())
                .limit(1)
            )
        ).scalars().first()
        if operador is None:
            reporte["errores"] = len(servicios)
            return reporte

        bajas = BajaService(self.db)
        for servicio in servicios:
            try:
                await FinanceService(
                    self.db
                ).normalizar_facturas_suspendidas(
                    servicio,
                    fecha_reactivacion=date.max,
                )
                baja = await bajas.crear(
                    cliente_id=servicio.cliente_id,
                    servicio_id=servicio.id,
                    motivo=(
                        f"Baja automática por falta de pago: más de {dias} "
                        "días suspendido"
                    ),
                    usuario=operador,
                )
                await bajas.sincronizar_mikrotik(baja.id)
                reporte["dados_de_baja"] += 1
            except Exception as exc:
                await self.db.rollback()
                reporte["errores"] += 1
                self.db.add(LogCronjobModel(
                    nivel="ERROR",
                    origen="BajaAutomatica",
                    mensaje=(
                        f"No se dio de baja el servicio {servicio.id}: {exc}"
                    ),
                ))
                await self.db.commit()
        return reporte

    async def recalcular_suspendidos(self) -> int:
        """Descuenta al día de hoy los días sin servicio de los suspendidos.

        Así el saldo guardado (listas de cobranza, bot, reportes) coincide
        con lo que se cobra al abrir al cliente.
        """
        servicios = (
            await self.db.execute(
                select(ServicioModel).where(
                    ServicioModel.estado == "suspendido"
                )
            )
        ).scalars().all()
        for servicio in servicios:
            await FinanceService(self.db).normalizar_facturas_suspendidas(
                servicio,
                fecha_reactivacion=date.today(),
            )
        return len(servicios)

    # ==========================================
    # COBRO TOTAL DEL CLIENTE (un solo cobro)
    # ==========================================
    @staticmethod
    def _mes_factura(factura) -> str:
        fecha = factura.periodo_desde or factura.fecha_vencimiento
        if fecha:
            return f"{MESES_EN_ESPANOL[fecha.month - 1].lower()} {fecha.year}"
        return factura.mes_correspondiente or "sin fecha"

    @classmethod
    def _texto_prorrateo(cls, desde, hasta) -> str:
        if desde and hasta:
            return (
                f"Prorrateo del {desde.strftime('%d/%m')} al "
                f"{hasta.strftime('%d/%m/%Y')}"
            )
        return "Prorrateo"

    @staticmethod
    def _variables_total_a_pagar(estado_cuenta: dict) -> dict:
        """{total_a_pagar} y {desglose_total} para los mensajes al cliente.

        El desglose solo se llena cuando hay algo más que una sola línea
        (meses atrasados, servicios extra, reconexión); así no repite el
        monto de la factura cuando es lo único que se debe.
        """
        total = Decimal(estado_cuenta.get("total") or 0)
        detalle = estado_cuenta.get("detalle") or []
        desglose = ""
        if len(detalle) > 1:
            lineas = [
                f"• {item['texto']}"
                + (" (este mes)" if item.get("actual") else "")
                + f": ${Decimal(item.get('monto') or 0):.2f}"
                for item in detalle
            ]
            desglose = "\n".join(
                [f"📋 *Total a pagar: ${total:.2f}*", *lineas]
            )
        return {
            "total_a_pagar": f"${total:.2f}",
            "desglose_total": desglose,
        }

    @staticmethod
    def _mensualidad_actual(facturas):
        """La mensualidad más reciente generada es la "actual" del cliente.

        `facturas` viene ordenada de la más antigua a la más reciente; las
        anteriores a ella son atrasadas. Los prorrateos no cuentan.
        """
        return next(
            (
                f for f in reversed(facturas)
                if f.tipo_factura == "mensual" and not f.es_prorrateada
            ),
            None,
        )

    @classmethod
    def _textos_factura(
        cls, factura, conceptos
    ) -> list[tuple[str, bool, Decimal]]:
        """Qué se cobra de esta factura, en palabras.

        Devuelve (texto, es_internet, monto pendiente) por renglón.
        """
        es_prorrateo = bool(
            factura.es_prorrateada or factura.tipo_factura == "prorrateo"
        )
        mensualidad = (
            cls._texto_prorrateo(factura.periodo_desde, factura.periodo_hasta)
            if es_prorrateo
            else f"Mensualidad de {cls._mes_factura(factura)}"
        )
        if not conceptos:
            saldo = Decimal(factura.saldo_pendiente or 0)
            if factura.tipo_factura in {"mensual", "prorrateo"} or es_prorrateo:
                return [(mensualidad, True, saldo)]
            return [(factura.concepto or "Cargo adicional", False, saldo)]
        textos = []
        # La mensualidad va primero y después los cargos (reconexión, extras).
        conceptos = sorted(
            conceptos,
            key=lambda c: str(c.tipo or "") != "internet",
        )
        for concepto in conceptos:
            saldo = Decimal(concepto.saldo_pendiente or 0)
            if concepto.tipo == "internet":
                textos.append((mensualidad, True, saldo))
            elif concepto.tipo == "internet_prorrateado":
                textos.append(("Prorrateo de instalación", False, saldo))
            elif concepto.tipo == "servicio_adicional":
                textos.append((f"Servicio extra: {concepto.concepto}", False, saldo))
            elif concepto.tipo == "reconexion":
                textos.append((
                    concepto.concepto
                    if concepto.concepto == CONCEPTO_RECONEXION_PROMESA
                    else "Cargo por reconexión",
                    False,
                    saldo,
                ))
            else:
                textos.append((concepto.concepto, False, saldo))
        return textos

    async def estado_cuenta_cliente(self, cliente_id: int) -> dict:
        """Total a pagar del cliente, listo para un solo cobro.

        Recalcula los días sin servicio de los servicios suspendidos (como si
        se reconectaran hoy) y suma todo lo pendiente: mensualidades,
        prorrateos, servicios extra y cargos.
        """
        suspendidos = (
            await self.db.execute(
                select(ServicioModel).where(
                    ServicioModel.cliente_id == cliente_id,
                    ServicioModel.estado == "suspendido",
                )
            )
        ).scalars().all()
        for servicio in suspendidos:
            await FinanceService(self.db).normalizar_facturas_suspendidas(
                servicio,
                fecha_reactivacion=date.today(),
            )
        await self.db.flush()

        facturas = (
            await self.db.execute(
                select(FacturaModel)
                .where(
                    FacturaModel.cliente_id == cliente_id,
                    FacturaModel.estado.in_(["pendiente", "vencida"]),
                    FacturaModel.saldo_pendiente > 0,
                )
                .order_by(
                    FacturaModel.fecha_vencimiento.asc(),
                    FacturaModel.id.asc(),
                )
            )
        ).scalars().all()

        hoy = date.today()
        varios_servicios = len({f.servicio_id for f in facturas}) > 1
        alias = {}
        if varios_servicios:
            alias = {
                s.id: s.alias
                for s in (
                    await self.db.execute(
                        select(ServicioModel).where(
                            ServicioModel.cliente_id == cliente_id
                        )
                    )
                ).scalars().all()
            }
        actual = self._mensualidad_actual(facturas)
        detalle: list[dict] = []
        for factura in facturas:
            conceptos = (
                await self.db.execute(
                    select(FacturaConceptoModel)
                    .where(
                        FacturaConceptoModel.factura_id == factura.id,
                        FacturaConceptoModel.saldo_pendiente > 0,
                    )
                    .order_by(FacturaConceptoModel.id)
                )
            ).scalars().all()
            sufijo = (
                f" ({alias.get(factura.servicio_id)})"
                if varios_servicios and alias.get(factura.servicio_id)
                else ""
            )
            for texto, es_internet, monto in self._textos_factura(
                factura, conceptos
            ):
                detalle.append({
                    "texto": texto + (sufijo if es_internet else ""),
                    "monto": monto,
                    "actual": es_internet and factura is actual,
                })
        incluye = []
        for item in detalle:
            if item["texto"] not in incluye:
                incluye.append(item["texto"])

        vencida = next(
            (
                f for f in facturas
                if f.afecta_corte
                and f.fecha_limite_corte
                and f.fecha_limite_corte < hoy
            ),
            None,
        )
        return {
            "cliente_id": cliente_id,
            "total": sum(
                (Decimal(f.saldo_pendiente or 0) for f in facturas),
                Decimal("0.00"),
            ),
            "incluye": incluye,
            "detalle": detalle,
            "facturas": [
                {"id": f.id, "saldo_pendiente": f.saldo_pendiente}
                for f in facturas
            ],
            "suspendido": bool(suspendidos),
            "factura_promesa_id": (vencida or (facturas[0] if facturas else None)).id
            if facturas
            else None,
        }

    async def registrar_pago_cliente(
        self,
        cliente_id: int,
        usuario_operador: UsuarioModel,
        metodo_pago: str,
        monto,
        referencia: str | None = None,
        clave_idempotencia: str | None = None,
    ) -> dict:
        """Un solo cobro por el total del cliente.

        El importe se aplica de lo más antiguo a lo más reciente; si no
        alcanza queda como abono y lo que sobra queda como saldo a favor.
        """
        recibido = FinanceService.dinero(monto)
        clave = (clave_idempotencia or "").strip()[:80] or None
        if clave:
            previos = (
                await self.db.execute(
                    select(PagoModel).where(
                        PagoModel.clave_idempotencia.like(f"{clave}:%"),
                        PagoModel.cliente_id == cliente_id,
                    )
                )
            ).scalars().all()
            if previos:
                return {
                    "status": "ok",
                    "idempotente": True,
                    "pago_ids": [p.id for p in previos],
                    "total_recibido": sum(
                        (Decimal(p.monto_total or 0) for p in previos),
                        Decimal("0.00"),
                    ),
                }

        estado = await self.estado_cuenta_cliente(cliente_id)
        facturas = estado["facturas"]
        if not facturas:
            raise ValueError("El cliente no tiene saldo pendiente")

        restante = recibido
        aplicados = []
        reactivado = False
        for indice, datos in enumerate(facturas):
            if restante <= 0:
                break
            saldo = Decimal(datos["saldo_pendiente"])
            ultima = indice == len(facturas) - 1
            importe = restante if ultima else min(restante, saldo)
            resultado = await self.registrar_pago_completo(
                factura_id=datos["id"],
                usuario_operador=usuario_operador,
                metodo_pago=metodo_pago,
                monto=importe,
                referencia=referencia,
                clave_idempotencia=(
                    f"{clave}:{datos['id']}" if clave else None
                ),
                confirmar_transaccion=False,
                enviar_notificacion=False,
            )
            restante -= importe
            reactivado = reactivado or bool(resultado.get("reactivado"))
            aplicados.append(resultado)
        await self.db.commit()

        cliente = await self.db.get(ClienteModel, cliente_id)
        pendientes = await self._listar_facturas_pendientes_cliente(cliente_id)
        saldo_restante = sum(
            (Decimal(f["saldo_pendiente"] or 0) for f in pendientes),
            Decimal("0.00"),
        )
        await self._notificar_cobro_total(
            cliente,
            [r["pago_id"] for r in aplicados],
            recibido,
            saldo_restante,
            reactivado,
            referencia,
        )
        return {
            "status": "ok",
            "idempotente": False,
            "pago_ids": [r["pago_id"] for r in aplicados],
            "total_recibido": recibido,
            "saldo_pendiente": saldo_restante,
            "saldo_a_favor": cliente.saldo_a_favor,
            "reactivado": reactivado,
        }

    async def _notificar_cobro_total(
        self,
        cliente,
        pago_ids: list[int],
        recibido: Decimal,
        saldo_restante: Decimal,
        reactivado: bool,
        referencia: str | None,
    ) -> None:
        """Un solo WhatsApp (y un solo recibo) por el cobro total."""
        if not cliente or not cliente.telefono or not pago_ids:
            return
        clave = f"cobro:{pago_ids[0]}"
        try:
            notificador = NotificationService(self.db)
            if reactivado:
                await notificador.notificar(
                    tipo_evento="reconexion",
                    cliente_id=cliente.id,
                    clave_dedupe=f"{clave}:reconexion",
                )
            pagos = (
                await self.db.execute(
                    select(PagoModel)
                    .where(PagoModel.id.in_(pago_ids))
                    .order_by(PagoModel.id)
                )
            ).scalars().all()
            ultima = await self.db.get(FacturaModel, pagos[-1].factura_id)
            if saldo_restante > 0:
                await notificador.notificar(
                    tipo_evento="abono_recibido",
                    cliente_id=cliente.id,
                    variables_extra={
                        **self._variables_detalle_factura(ultima),
                        "monto_pagado": f"${recibido:.2f}",
                        "referencia": (
                            "Abono registrado. (Resta por pagar: "
                            f"${saldo_restante:.2f})"
                        ),
                    },
                    clave_dedupe=f"{clave}:abono",
                )
                return
            conceptos_pagados = (
                await self.db.execute(
                    select(
                        FacturaConceptoModel.concepto,
                        func.sum(PagoConceptoModel.monto_aplicado),
                    )
                    .join(
                        PagoConceptoModel,
                        PagoConceptoModel.concepto_id == FacturaConceptoModel.id,
                    )
                    .where(PagoConceptoModel.pago_id.in_(pago_ids))
                    .group_by(FacturaConceptoModel.concepto)
                )
            ).all()
            marca = await self.db.get(ConfiguracionSistema, 1)
            ruta_pdf = await generar_recibo_pdf(
                nombre_cliente=cliente.nombre,
                monto=recibido,
                concepto="Pago de servicios",
                descripcion=ultima.descripcion or ultima.detalles or "Pago de servicio",
                fecha_pago=pagos[-1].fecha_pago,
                folio=ultima.id,
                nueva_fecha_vencimiento=(
                    ultima.fecha_vencimiento + relativedelta(months=1)
                    if ultima.tipo_factura in {"mensual", "prorrateo"}
                    else None
                ),
                telefono_cliente=cliente.telefono,
                metodo_pago=pagos[-1].metodo_pago,
                periodo_desde=ultima.periodo_desde,
                periodo_hasta=ultima.periodo_hasta,
                total_factura=recibido,
                conceptos_pagados=[
                    {"concepto": concepto, "monto": monto}
                    for concepto, monto in conceptos_pagados
                ],
                empresa_nombre=marca.empresa_nombre if marca else "FdezNet",
                color_primario=marca.color_primario if marca else "#1e3a8a",
                color_secundario=marca.color_secundario if marca else "#2563eb",
                pie_recibo=marca.pie_recibo if marca else None,
                empresa_telefono=marca.empresa_telefono if marca else None,
                empresa_email=marca.empresa_email if marca else None,
                empresa_direccion=marca.empresa_direccion if marca else None,
            )
            await notificador.notificar(
                tipo_evento="pago_recibido",
                cliente_id=cliente.id,
                variables_extra={
                    **self._variables_detalle_factura(ultima),
                    "monto_pagado": f"${recibido:.2f}",
                    "referencia": referencia or "N/A",
                },
                ruta_pdf=ruta_pdf,
                clave_dedupe=f"{clave}:recibo",
            )
        except Exception:
            logger.exception(
                "Error al notificar el cobro total del cliente %s", cliente.id
            )

    async def registrar_pago_completo(
        self,
        factura_id: int,
        usuario_operador: UsuarioModel,
        metodo_pago: str,
        monto,
        referencia: str = None,
        clave_idempotencia: str = None,
        concepto_ids: list[int] | None = None,
        confirmar_transaccion: bool = True,
        enviar_notificacion: bool = True,
    ):
        finanzas = FinanceService(self.db)
        factura_cobrable, _, _ = await self.preparar_factura_cobrable(
            factura_id,
            fecha_reactivacion=date.today(),
        )
        if factura_cobrable.id != factura_id:
            raise ValueError(
                f"Primero debe cobrarse la factura #{factura_cobrable.id}, "
                f"que es la deuda real más antigua del servicio. "
                f"Saldo: ${factura_cobrable.saldo_pendiente}."
            )
        nuevo_pago, factura, cliente, repetido = await finanzas.registrar_pago(
            factura_id=factura_cobrable.id,
            usuario_id=usuario_operador.id,
            metodo_pago=metodo_pago,
            monto=monto,
            referencia=referencia,
            clave_idempotencia=clave_idempotencia,
        )
        if repetido:
            facturas_pendientes = await self._listar_facturas_pendientes_cliente(
                cliente.id
            )
            return {
                "status": "ok",
                "idempotente": True,
                "pago_id": nuevo_pago.id,
                "factura_id_aplicada": factura.id,
                "factura_liquidada": factura.saldo_pendiente == 0,
                "saldo_pendiente": factura.saldo_pendiente,
                "facturas_pendientes_cant": len(facturas_pendientes),
                "facturas_pendientes": facturas_pendientes,
            }

        internet_liquidado = await self._distribuir_pago_en_conceptos(
            factura,
            nuevo_pago,
            concepto_ids,
        )

        stmt_c = select(ClienteModel).options(
            selectinload(ClienteModel.plan),
            selectinload(ClienteModel.plantilla),
            joinedload(ClienteModel.router),
        ).where(ClienteModel.id == cliente.id)
        cliente = (await self.db.execute(stmt_c)).scalar_one()

        servicio = (
            await self.db.get(ServicioModel, factura.servicio_id)
            if factura.servicio_id
            else None
        )
        objetivo = servicio or cliente
        estado_previo = objetivo.estado
        reactivado = False
        pago_completado = nuevo_pago.saldo_posterior == 0
        
        # Reconexión automática SOLO si la factura quedó pagada por completo
        # FACTURACION_ISP_V2_SAFE_REACTIVATION
        if internet_liquidado and estado_previo == "suspendido":
            await self.db.flush()
            tiene_otra_deuda = (
                await self._servicio_tiene_deuda_pendiente(
                    factura,
                    excluir_factura_id=factura.id,
                )
            )
            if not tiene_otra_deuda:
                reactivado = await self._reactivar_en_mikrotik(
                    objetivo
                )
                if reactivado:
                    if servicio:
                        await self._cerrar_suspension_facturacion(
                            servicio,
                            date.today(),
                            "pago",
                        )
                    await self._actualizar_estado_servicio_factura(
                        factura,
                        "activo",
                    )
                    await self._sincronizar_estado_cliente(cliente.id)
                    if servicio:
                        servicio.ultima_reactivacion_origen = (
                            "bot"
                            if nuevo_pago.metodo_pago == "autovalidado"
                            else "manual"
                        )
                        servicio.ultima_reactivacion_en = datetime.now()

        if confirmar_transaccion:
            await self.db.commit()
        else:
            await self.db.flush()

        # 👇 🚀 LOGICA DE WHATSAPP 👇
        notificacion_pago_encolada = False
        if enviar_notificacion and cliente.telefono:
            try:
                notificador = NotificationService(self.db)

                if reactivado:
                    await notificador.notificar(
                        tipo_evento="reconexion",
                        cliente_id=cliente.id,
                        clave_dedupe=f"pago:{nuevo_pago.id}:reconexion",
                    )
                
                if pago_completado:
                    # Si liquidó, se le manda su PDF
                    prox_venc = (
                        factura.fecha_vencimiento + relativedelta(months=1)
                        if factura.tipo_factura in {"mensual", "prorrateo"}
                        else None
                    )
                    concepto_recibo = (
                        factura.concepto
                        or f"Mensualidad de internet - {factura.plan_snapshot}"
                    )
                    descripcion_recibo = (
                        factura.descripcion
                        or factura.detalles
                        or factura.mes_correspondiente
                        or "Pago de servicio"
                    )
                    conceptos_pagados = (
                        await self.db.execute(
                            select(
                                FacturaConceptoModel.concepto,
                                PagoConceptoModel.monto_aplicado,
                            )
                            .join(
                                PagoConceptoModel,
                                PagoConceptoModel.concepto_id == FacturaConceptoModel.id,
                            )
                            .where(PagoConceptoModel.pago_id == nuevo_pago.id)
                        )
                    ).all()
                    marca = await self.db.get(ConfiguracionSistema, 1)
                    ruta_pdf = await generar_recibo_pdf(
                        nombre_cliente=cliente.nombre,
                        monto=nuevo_pago.monto_total,
                        concepto=concepto_recibo,
                        descripcion=descripcion_recibo,
                        fecha_pago=nuevo_pago.fecha_pago,
                        folio=factura.id,
                        nueva_fecha_vencimiento=prox_venc,
                        telefono_cliente=cliente.telefono,
                        metodo_pago=nuevo_pago.metodo_pago,
                        periodo_desde=factura.periodo_desde,
                        periodo_hasta=factura.periodo_hasta,
                        dias_con_servicio=factura.dias_con_servicio,
                        dias_sin_servicio=factura.dias_sin_servicio,
                        monto_servicio_original=factura.monto_servicio_original,
                        ajuste_suspension=factura.ajuste_suspension,
                        cargos_adicionales=factura.cargos_adicionales_total,
                        total_factura=factura.total,
                        conceptos_pagados=[
                            {"concepto": fila.concepto, "monto": fila.monto_aplicado}
                            for fila in conceptos_pagados
                        ],
                        empresa_nombre=marca.empresa_nombre if marca else "FdezNet",
                        color_primario=marca.color_primario if marca else "#1e3a8a",
                        color_secundario=marca.color_secundario if marca else "#2563eb",
                        pie_recibo=marca.pie_recibo if marca else None,
                        empresa_telefono=marca.empresa_telefono if marca else None,
                        empresa_email=marca.empresa_email if marca else None,
                        empresa_direccion=marca.empresa_direccion if marca else None,
                    )
                    notificacion_pago_encolada = await notificador.notificar(
                        tipo_evento="pago_recibido", 
                        cliente_id=cliente.id,
                        variables_extra={
                            **self._variables_detalle_factura(factura),
                            "monto_pagado": f"${nuevo_pago.monto_total:.2f}",
                            "referencia": referencia or "N/A",
                        },
                        ruta_pdf=ruta_pdf,
                        clave_dedupe=f"pago:{nuevo_pago.id}:recibo",
                    )
                else:
                    # Si solo abonó una parte, se manda este aviso sencillo sin PDF
                    notificacion_pago_encolada = await notificador.notificar(
                        tipo_evento="abono_recibido", # 👈 Aquí hace match con la BD
                        cliente_id=cliente.id,
                        variables_extra={
                            **self._variables_detalle_factura(factura),
                            "monto_pagado": f"${nuevo_pago.monto_total:.2f}",
                            "referencia": f"Abono parcial registrado. (Resta por pagar: ${factura.saldo_pendiente})"
                        },
                        clave_dedupe=f"pago:{nuevo_pago.id}:abono",
                    )
                
            except Exception:
                logger.exception(
                    "Error al crear la confirmación del pago %s para el cliente %s",
                    nuevo_pago.id,
                    cliente.id,
                )

        facturas_pendientes = await self._listar_facturas_pendientes_cliente(
            cliente.id
        )
        return {
            "status": "ok",
            "idempotente": False,
            "pago_id": nuevo_pago.id,
            "factura_id_aplicada": factura.id,
            "factura_liquidada": pago_completado,
            "notificacion_pago_encolada": notificacion_pago_encolada,
            "saldo_pendiente": factura.saldo_pendiente,
            "saldo_a_favor": cliente.saldo_a_favor,
            "reactivado": reactivado,
            "facturas_pendientes_cant": len(facturas_pendientes),
            "facturas_pendientes": facturas_pendientes,
        }

    async def _distribuir_pago_en_conceptos(
        self,
        factura: FacturaModel,
        pago: PagoModel,
        concepto_ids: list[int] | None,
    ) -> bool:
        """Aplica el importe por renglón y devuelve si internet quedó liquidado."""
        consulta = select(FacturaConceptoModel).where(
            FacturaConceptoModel.factura_id == factura.id,
            FacturaConceptoModel.saldo_pendiente > 0,
        )
        if concepto_ids:
            consulta = consulta.where(FacturaConceptoModel.id.in_(concepto_ids))
        conceptos = (
            await self.db.execute(
                consulta.order_by(
                    FacturaConceptoModel.afecta_corte.desc(),
                    FacturaConceptoModel.fecha_cargo.asc(),
                    FacturaConceptoModel.id.asc(),
                ).with_for_update()
            )
        ).scalars().all()
        if concepto_ids and len({item.id for item in conceptos}) != len(set(concepto_ids)):
            raise ValueError("Algún concepto seleccionado no pertenece a la factura o ya está pagado")

        restante = Decimal(pago.monto_aplicado or 0)
        saldo_seleccionado = sum(
            (Decimal(item.saldo_pendiente or 0) for item in conceptos),
            Decimal("0.00"),
        )
        if concepto_ids and restante > saldo_seleccionado:
            raise ValueError(
                f"El monto excede el saldo de los conceptos seleccionados (${saldo_seleccionado})"
            )
        for concepto in conceptos:
            if restante <= 0:
                break
            saldo = Decimal(concepto.saldo_pendiente or 0)
            aplicado = min(restante, saldo)
            concepto.saldo_pendiente = (saldo - aplicado).quantize(Decimal("0.01"))
            concepto.estado = "pagado" if concepto.saldo_pendiente == 0 else "abonado"
            self.db.add(PagoConceptoModel(
                pago_id=pago.id,
                concepto_id=concepto.id,
                monto_aplicado=aplicado,
            ))
            restante -= aplicado

        total_conceptos = (
            await self.db.execute(
                select(func.count(FacturaConceptoModel.id)).where(
                    FacturaConceptoModel.factura_id == factura.id,
                )
            )
        ).scalar_one()
        if total_conceptos == 0:
            return Decimal(factura.saldo_pendiente or 0) == 0
        if Decimal(factura.saldo_pendiente or 0) == 0:
            # El saldo de la factura manda: el ajuste por días suspendidos y
            # los descuentos reducen el total sin tocar los renglones, y ese
            # remanente no es deuda que deba impedir la reconexión.
            factura.afecta_corte = False
            return True

        internet_pendiente = (
            await self.db.execute(
                select(func.count(FacturaConceptoModel.id)).where(
                    FacturaConceptoModel.factura_id == factura.id,
                    FacturaConceptoModel.afecta_corte.is_(True),
                    FacturaConceptoModel.saldo_pendiente > 0,
                )
            )
        ).scalar_one()
        if internet_pendiente == 0:
            factura.afecta_corte = False
        return internet_pendiente == 0

    # ==========================================
    # HELPERS
    # ==========================================

    async def preparar_factura_cobrable(
        self,
        factura_id: int,
        *,
        fecha_reactivacion: date | None = None,
    ):
        """Cierra ciclos sin servicio y devuelve la deuda real más antigua."""
        solicitada = await self.db.get(FacturaModel, factura_id)
        if not solicitada:
            raise ValueError("Factura no encontrada")

        servicio = (
            await self.db.get(ServicioModel, solicitada.servicio_id)
            if solicitada.servicio_id
            else None
        )
        if servicio and servicio.estado == "suspendido":
            await FinanceService(
                self.db
            ).normalizar_facturas_suspendidas(
                servicio,
                fecha_reactivacion=fecha_reactivacion,
            )

        condiciones = [
            FacturaModel.estado.in_(["pendiente", "vencida"]),
            FacturaModel.saldo_pendiente > 0,
        ]
        if not solicitada.afecta_corte:
            condiciones.append(FacturaModel.id == solicitada.id)
        else:
            condiciones.append(FacturaModel.afecta_corte.is_(True))
        if solicitada.servicio_id:
            condiciones.append(
                FacturaModel.servicio_id == solicitada.servicio_id
            )
        else:
            condiciones.extend([
                FacturaModel.cliente_id == solicitada.cliente_id,
                FacturaModel.servicio_id.is_(None),
            ])

        cobrable = (
            await self.db.execute(
                select(FacturaModel)
                .where(*condiciones)
                .order_by(
                    FacturaModel.fecha_vencimiento.asc(),
                    FacturaModel.id.asc(),
                )
                .with_for_update()
            )
        ).scalars().first()
        if not cobrable:
            raise ValueError(
                "No quedan facturas con deuda real. Los ciclos sin servicio "
                "se cerraron sin cargo."
            )
        return cobrable, solicitada, servicio

    async def _listar_facturas_pendientes_cliente(self, cliente_id: int):
        facturas = (
            await self.db.execute(
                select(FacturaModel)
                .where(
                    FacturaModel.cliente_id == cliente_id,
                    FacturaModel.estado.in_(["pendiente", "vencida"]),
                    FacturaModel.saldo_pendiente > 0,
                )
                .order_by(
                    FacturaModel.fecha_vencimiento.asc(),
                    FacturaModel.id.asc(),
                )
            )
        ).scalars().all()
        return [
            {
                "id": item.id,
                "concepto": (
                    item.concepto
                    or item.detalles
                    or item.mes_correspondiente
                    or f"Factura #{item.id}"
                ),
                "descripcion": item.descripcion,
                "fecha_vencimiento": item.fecha_vencimiento,
                "saldo_pendiente": item.saldo_pendiente,
            }
            for item in facturas
        ]

    # FACTURACION_ISP_V2_STATE_SYNC_HELPERS
    async def _actualizar_estado_servicio_factura(
        self,
        factura,
        estado: str,
    ) -> int:
        actualizados = 0

        if getattr(factura, "servicio_id", None):
            servicio = await self.db.get(
                ServicioModel,
                factura.servicio_id,
            )

            if (
                servicio
                and servicio.estado != "cancelado"
                and servicio.estado != estado
            ):
                servicio.estado = estado
                actualizados += 1

            return actualizados

        stmt = select(ServicioModel).where(
            ServicioModel.cliente_id == factura.cliente_id,
            ServicioModel.estado.in_(["activo", "suspendido"]),
        )
        servicios = (
            await self.db.execute(stmt)
        ).scalars().all()

        for servicio in servicios:
            if servicio.estado != estado:
                servicio.estado = estado
                actualizados += 1

        return actualizados

    async def _cliente_tiene_deuda_pendiente(
        self,
        cliente_id: int,
        excluir_factura_id: int | None = None,
    ) -> bool:
        condiciones = [
            FacturaModel.cliente_id == cliente_id,
            FacturaModel.estado.in_(["pendiente", "vencida"]),
            FacturaModel.saldo_pendiente > 0,
        ]

        if excluir_factura_id is not None:
            condiciones.append(
                FacturaModel.id != excluir_factura_id
            )

        stmt = select(func.count(FacturaModel.id)).where(
            *condiciones
        )
        cantidad = (
            await self.db.execute(stmt)
        ).scalar_one()

        return cantidad > 0

    @staticmethod
    def _condiciones_deuda_cortable(hoy: date) -> list:
        """Deuda que justifica un corte.

        El corte y la reactivación usan este mismo criterio: si no, una
        mensualidad nueva que todavía no vence mantiene suspendido a un
        cliente que ya pagó todo lo vencido.
        """
        return [
            FacturaModel.estado.in_(["pendiente", "vencida"]),
            FacturaModel.saldo_pendiente > 0,
            FacturaModel.afecta_corte.is_(True),
            FacturaModel.tipo_factura != "prorrateo",
            or_(
                and_(
                    # La fecha límite es el último día completo para pagar;
                    # el corte procede a partir del día siguiente.
                    FacturaModel.fecha_limite_corte < hoy,
                    FacturaModel.es_promesa_activa.is_(False),
                ),
                and_(
                    FacturaModel.es_promesa_activa.is_(True),
                    FacturaModel.fecha_promesa_pago < hoy,
                ),
            ),
        ]

    async def _servicio_tiene_deuda_pendiente(
        self,
        factura,
        excluir_factura_id: int | None = None,
    ) -> bool:
        condiciones = self._condiciones_deuda_cortable(date.today())
        if factura.servicio_id:
            condiciones.append(
                FacturaModel.servicio_id == factura.servicio_id
            )
        else:
            condiciones.append(
                FacturaModel.cliente_id == factura.cliente_id
            )
        if excluir_factura_id is not None:
            condiciones.append(FacturaModel.id != excluir_factura_id)
        cantidad = (
            await self.db.execute(
                select(func.count(FacturaModel.id)).where(*condiciones)
            )
        ).scalar_one()
        return cantidad > 0

    async def _sincronizar_estado_cliente(self, cliente_id: int):
        cliente = await self.db.get(ClienteModel, cliente_id)
        estados = (
            await self.db.execute(
                select(ServicioModel.estado).where(
                    ServicioModel.cliente_id == cliente_id,
                    ServicioModel.estado != "cancelado",
                )
            )
        ).scalars().all()
        if "activo" in estados:
            cliente.estado = "activo"
        elif "suspendido" in estados:
            cliente.estado = "suspendido"
        elif "pendiente_instalacion" in estados:
            cliente.estado = "pendiente_instalacion"
        else:
            cliente.estado = "cancelado"

    async def _abrir_suspension_facturacion(
        self,
        servicio: ServicioModel,
        factura: FacturaModel,
        motivo: str,
    ) -> SuspensionFacturacionModel:
        abierta = (
            await self.db.execute(
                select(SuspensionFacturacionModel).where(
                    SuspensionFacturacionModel.servicio_id == servicio.id,
                    SuspensionFacturacionModel.fecha_fin.is_(None),
                )
            )
        ).scalars().first()
        if abierta:
            return abierta
        abierta = SuspensionFacturacionModel(
            servicio_id=servicio.id,
            factura_origen_id=factura.id,
            fecha_inicio=date.today(),
            motivo_inicio=motivo,
        )
        servicio.fecha_suspension_facturacion = date.today()
        self.db.add(abierta)
        await self.db.flush()
        return abierta

    async def _cerrar_suspension_facturacion(
        self,
        servicio: ServicioModel,
        fecha_reactivacion: date,
        motivo: str,
    ) -> None:
        abiertas = (
            await self.db.execute(
                select(SuspensionFacturacionModel)
                .where(
                    SuspensionFacturacionModel.servicio_id == servicio.id,
                    SuspensionFacturacionModel.fecha_fin.is_(None),
                )
                .with_for_update()
            )
        ).scalars().all()
        ultimo_dia_sin_servicio = fecha_reactivacion - timedelta(days=1)
        for intervalo in abiertas:
            if ultimo_dia_sin_servicio < intervalo.fecha_inicio:
                await self.db.delete(intervalo)
            else:
                intervalo.fecha_fin = ultimo_dia_sin_servicio
                intervalo.motivo_fin = motivo
        servicio.fecha_suspension_facturacion = None
        servicio.fecha_ultima_reactivacion = fecha_reactivacion

    async def _reactivar_en_mikrotik(self, cliente):
        if not cliente.router_id or not cliente.ip_asignada:
            return False
        try:
            router = await self.db.get(RouterModel, cliente.router_id)
            if not router or not router.is_active:
                return False
            mk = MikroTikService(
                router.ip_vpn,
                router.user_api,
                router.pass_api,
                router.port_api,
            )
            modo = getattr(
                router.tipo_seguridad,
                "value",
                router.tipo_seguridad,
            )
            if str(modo).lower() == "dhcp":
                mac = normalizar_mac(cliente.mac_address)
                if not mac:
                    return False
                plan = (
                    await self.db.get(PlanModel, cliente.plan_id)
                    if cliente.plan_id
                    else None
                )
                if not plan:
                    return False
                mk.crear_actualizar_lease_dhcp(
                    mac,
                    cliente.ip_asignada,
                    formatear_rate_limit_dhcp(plan),
                    f"Reactivación de servicio {getattr(cliente, 'id', '')}",
                )
                if not mk.activar_desactivar_dhcp(mac, blocked=False):
                    return False
                return mk.gestionar_corte_cliente(
                    cliente.ip_asignada, suspender=False
                ) is True
            return (
                mk.reactivar_cliente(
                    cliente.ip_asignada,
                    cliente.user_pppoe,
                )
                is True
            )
        except Exception as e:
            print(f"⚠️ Error reactivando en MK: {e}")
            return False

    async def registrar_promesa_y_reactivar(
        self,
        factura_id: int,
        fecha_promesa: date,
        usuario_id: int | None,
        notas: str | None = None,
        enviar_notificaciones: bool = True,
        origen: str = "manual",
    ):
        """Registra una promesa con el mismo flujo para todos los canales."""
        factura_cobrable, _, _ = await self.preparar_factura_cobrable(
            factura_id,
            fecha_reactivacion=date.today(),
        )
        promesa, factura, cliente, politica = (
            await FinanceService(self.db).registrar_promesa(
                factura_cobrable.id,
                fecha_promesa,
                usuario_id,
                notas,
                origen,
            )
        )

        reactivado = False
        servicio = (
            await self.db.get(ServicioModel, factura.servicio_id)
            if factura.servicio_id
            else None
        )
        objetivo = servicio or cliente
        if objetivo.estado == "suspendido" and politica.permite_reconexion:
            reactivado = await self._reactivar_en_mikrotik(objetivo)
            if not reactivado:
                raise ValueError(
                    "MikroTik no confirmó la reactivación; "
                    "la promesa no fue guardada."
                )

            await self._actualizar_estado_servicio_factura(
                factura,
                "activo",
            )
            if servicio:
                await self._cerrar_suspension_facturacion(
                    servicio,
                    date.today(),
                    "promesa_pago",
                )
                await FinanceService(
                    self.db
                ).recalcular_factura_por_suspension(
                    factura,
                    servicio,
                    fecha_reactivacion=date.today(),
                )
                servicio.ultima_reactivacion_origen = promesa.origen
                servicio.ultima_reactivacion_en = datetime.now()
            promesa.servicio_reactivado = True
            promesa.reactivado_en = datetime.now()
            await self._sincronizar_estado_cliente(cliente.id)

        await self.db.commit()

        if enviar_notificaciones and cliente.telefono:
            notificador = NotificationService(self.db)
            fecha_promesa_str = fecha_promesa.strftime("%d/%m/%Y")

            try:
                if reactivado:
                    await notificador.notificar(
                        tipo_evento="reconexion",
                        cliente_id=cliente.id,
                        clave_dedupe=f"promesa:{promesa.id}:reconexion",
                    )

                await notificador.notificar(
                    tipo_evento="promesa_pago",
                    cliente_id=cliente.id,
                    variables_extra={
                        **self._variables_detalle_factura(factura),
                        "fecha_limite_promesa": fecha_promesa_str,
                        "monto_promesa": (
                            f"${float(factura.saldo_pendiente or 0):.2f}"
                        ),
                    },
                    clave_dedupe=f"promesa:{promesa.id}:confirmacion",
                )
            except Exception as exc:
                print(f"⚠️ Error notificación promesa: {exc}")

        return promesa, factura, cliente, politica, reactivado

    async def listar_facturas_por_permisos(self, usuario_id_solicitante: int, cliente_id: Optional[int] = None, router_id: Optional[int] = None):
        # ... (Tu código de permisos se mantiene igual) ...
        stmt_user = select(UsuarioModel).options(selectinload(UsuarioModel.routers_asignados)).where(UsuarioModel.id == usuario_id_solicitante)
        usuario = (await self.db.execute(stmt_user)).scalar_one()
        query = (
            select(FacturaModel)
            .join(ClienteModel)
            .outerjoin(
                ServicioModel,
                ServicioModel.id == FacturaModel.servicio_id,
            )
            .options(
                joinedload(FacturaModel.cliente).joinedload(
                    ClienteModel.router
                ),
                joinedload(FacturaModel.servicio).joinedload(
                    ServicioModel.router
                ),
            )
        )
        if cliente_id: query = query.where(FacturaModel.cliente_id == cliente_id)
        if router_id:
            query = query.where(
                or_(
                    ServicioModel.router_id == router_id,
                    and_(
                        FacturaModel.servicio_id.is_(None),
                        ClienteModel.router_id == router_id,
                    ),
                )
            )
        if usuario.rol != 'admin':
            ids_permitidos = [r.id for r in usuario.routers_asignados]
            if not ids_permitidos: return [] 
            query = query.where(
                or_(
                    ServicioModel.router_id.in_(ids_permitidos),
                    and_(
                        FacturaModel.servicio_id.is_(None),
                        ClienteModel.router_id.in_(ids_permitidos),
                    ),
                )
            )
        query = query.order_by(FacturaModel.id.desc()).limit(200)
        return (await self.db.execute(query)).scalars().all()
