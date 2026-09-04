from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Optional, List
from dateutil.relativedelta import relativedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_, desc, func, extract
from sqlalchemy.orm import joinedload, selectinload

# Modelos
from src.infrastructure.models import (
    ClienteModel, FacturaModel, FacturaConceptoModel, PagoConceptoModel, PagoModel,
    UsuarioModel, PlanModel, RouterModel,
    ServicioModel,
    SuspensionFacturacionModel,
)

# Servicios e Helpers
from src.infrastructure.mikrotik_service import MikroTikService
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


class BillingService:
    def __init__(self, db: AsyncSession):
        self.db = db

    @staticmethod
    def _variables_detalle_factura(factura):
        """Variables comunes para recibos y plantillas de WhatsApp."""
        def fecha(valor):
            return valor.strftime("%d/%m/%Y") if valor else "N/A"

        def dinero(valor):
            return f"${float(valor or 0):.2f}"

        return {
            "detalle_cobro": factura.descripcion or "Detalle no disponible",
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
                afecta_corte=True,
                fecha_cargo=periodo.periodo_desde,
            )
            self.db.add(concepto_internet)

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
                and (
                    servicio.estado != "suspendido"
                    or periodo.periodo_hasta < hoy
                )
            )
            if cliente.telefono and debe_notificar:
                exito = await notificador.notificar(
                    "nueva_factura",
                    cliente.id,
                    variables_extra={
                        **self._variables_detalle_factura(nueva_factura),
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
                    await notificador.notificar(
                        tipo_evento="recordatorio_pago", 
                        cliente_id=cliente.id,
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
                FacturaModel.estado.in_(["pendiente", "vencida"]),
                FacturaModel.saldo_pendiente > 0,
                FacturaModel.afecta_corte.is_(True),
                ClienteModel.estado != "eliminado",
                or_(
                    and_(
                        FacturaModel.fecha_limite_corte <= hoy,
                        FacturaModel.es_promesa_activa.is_(False),
                    ),
                    and_(
                        FacturaModel.es_promesa_activa.is_(True),
                        FacturaModel.fecha_promesa_pago < hoy,
                    ),
                ),
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

    async def registrar_pago_completo(
        self,
        factura_id: int,
        usuario_operador: UsuarioModel,
        metodo_pago: str,
        monto,
        referencia: str = None,
        clave_idempotencia: str = None,
        concepto_ids: list[int] | None = None,
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

        await self.db.commit()

        # 👇 🚀 LOGICA DE WHATSAPP 👇
        if cliente.telefono:
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
                    )
                    await notificador.notificar(
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
                    await notificador.notificar(
                        tipo_evento="abono_recibido", # 👈 Aquí hace match con la BD
                        cliente_id=cliente.id,
                        variables_extra={
                            **self._variables_detalle_factura(factura),
                            "monto_pagado": f"${nuevo_pago.monto_total:.2f}",
                            "referencia": f"Abono parcial registrado. (Resta por pagar: ${factura.saldo_pendiente})"
                        },
                        clave_dedupe=f"pago:{nuevo_pago.id}:abono",
                    )
                
            except Exception as e:
                print(f"⚠️ Error al notificar pago: {e}")

        facturas_pendientes = await self._listar_facturas_pendientes_cliente(
            cliente.id
        )
        return {
            "status": "ok",
            "idempotente": False,
            "pago_id": nuevo_pago.id,
            "factura_id_aplicada": factura.id,
            "factura_liquidada": pago_completado,
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

    async def _servicio_tiene_deuda_pendiente(
        self,
        factura,
        excluir_factura_id: int | None = None,
    ) -> bool:
        condiciones = [
            FacturaModel.estado.in_(["pendiente", "vencida"]),
            FacturaModel.saldo_pendiente > 0,
            FacturaModel.afecta_corte.is_(True),
        ]
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
