from datetime import date, datetime, timedelta
from typing import Optional, List
from dateutil.relativedelta import relativedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_, desc, func, extract
from sqlalchemy.orm import joinedload, selectinload

# Modelos
from src.infrastructure.models import (
    ClienteModel, FacturaModel, PagoModel, 
    UsuarioModel, PlantillaFacturacionModel, PlanModel, RouterModel,
    ServicioModel,
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

class BillingService:
    def __init__(self, db: AsyncSession):
        self.db = db

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
                selectinload(ServicioModel.plantilla),
                selectinload(ServicioModel.plan),
            )
            .where(
                ServicioModel.estado.in_(["activo", "suspendido"]),
                ServicioModel.plantilla_id.isnot(None),
                ServicioModel.plan_id.isnot(None),
            )
        )

        if dia_objetivo:
            stmt = stmt.join(
                PlantillaFacturacionModel,
                PlantillaFacturacionModel.id
                == ServicioModel.plantilla_id,
            ).where(
                PlantillaFacturacionModel.dia_pago == dia_objetivo
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
            "omitidos_sin_proxima_facturacion": 0,
            "omitidos_modalidad_pendiente": 0,
            "omitidos_no_toca_emitir": 0,
            "omitidos_factura_existente": 0,
        }

        for servicio in servicios:
            reporte["total_procesados"] += 1
            cliente = servicio.cliente
            plantilla = servicio.plantilla
            plan = servicio.plan
            if servicio.ciclo_facturacion == CicloFacturacion.aniversario:
                reporte["omitidos_modalidad_pendiente"] += 1
                continue
            if servicio.proxima_facturacion is None:
                reporte["omitidos_sin_proxima_facturacion"] += 1
                continue

            tipo_snapshot = servicio.tipo_facturacion.value if hasattr(servicio.tipo_facturacion, "value") else str(servicio.tipo_facturacion)
            ciclo_snapshot = servicio.ciclo_facturacion.value if hasattr(servicio.ciclo_facturacion, "value") else str(servicio.ciclo_facturacion)

            if tipo_snapshot not in {"prepago", "postpago"}:
                reporte["omitidos_modalidad_pendiente"] += 1
                continue

            dia_ciclo = servicio.dia_vencimiento or plantilla.dia_pago or 1
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

            if not dia_objetivo and hoy < fecha_generacion:
                reporte["omitidos_no_toca_emitir"] += 1
                continue

            stmt_dup = select(FacturaModel).where(
                and_(
                    FacturaModel.servicio_id == servicio.id,
                    FacturaModel.periodo_desde == periodo.periodo_desde,
                    FacturaModel.periodo_hasta == periodo.periodo_hasta,
                )
            )
            if (await self.db.execute(stmt_dup)).scalars().first():
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
                    mes_actual_str = periodo.periodo_desde.strftime("%B %Y").capitalize()
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
                detalles=detalles,
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
            )
            self.db.add(nueva_factura)
            await self.db.flush()

            servicio.proxima_facturacion = periodo.siguiente_facturacion
            reporte["facturas_generadas"] += 1
            if periodo.es_prorrateada:
                reporte["facturas_prorrateadas"] += 1
            if tipo_snapshot == "postpago":
                reporte["facturas_postpago"] += 1
            else:
                reporte["facturas_prepago"] += 1

            if cliente.telefono:
                exito = await notificador.notificar(
                    "nueva_factura",
                    cliente.id,
                    variables_extra={
                        "monto": f"${periodo.total}",
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
    ):
        finanzas = FinanceService(self.db)
        nuevo_pago, factura, cliente, repetido = await finanzas.registrar_pago(
            factura_id=factura_id,
            usuario_id=usuario_operador.id,
            metodo_pago=metodo_pago,
            monto=monto,
            referencia=referencia,
            clave_idempotencia=clave_idempotencia,
        )
        if repetido:
            return {
                "status": "ok",
                "idempotente": True,
                "pago_id": nuevo_pago.id,
                "factura_liquidada": factura.saldo_pendiente == 0,
                "saldo_pendiente": factura.saldo_pendiente,
            }

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
        if pago_completado and estado_previo == "suspendido":
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
                    prox_venc = (factura.fecha_vencimiento + relativedelta(months=1)).strftime("%d/%m/%Y")
                    ruta_pdf = await generar_recibo_pdf(
                        nombre_cliente=cliente.nombre,
                        monto=nuevo_pago.monto_total,
                        concepto=f"MENSUALIDAD INTERNET - {factura.plan_snapshot}",
                        fecha_pago=nuevo_pago.fecha_pago,
                        folio=factura.id,
                        nueva_fecha_vencimiento=prox_venc,
                        telefono_cliente=cliente.telefono,
                        metodo_pago=nuevo_pago.metodo_pago,
                    )
                    await notificador.notificar(
                        tipo_evento="pago_recibido", 
                        cliente_id=cliente.id,
                        variables_extra={
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
                            "monto_pagado": f"${nuevo_pago.monto_total:.2f}",
                            "referencia": f"Abono parcial registrado. (Resta por pagar: ${factura.saldo_pendiente})"
                        },
                        clave_dedupe=f"pago:{nuevo_pago.id}:abono",
                    )
                
            except Exception as e:
                print(f"⚠️ Error al notificar pago: {e}")

        return {
            "status": "ok",
            "idempotente": False,
            "pago_id": nuevo_pago.id,
            "factura_liquidada": pago_completado,
            "saldo_pendiente": factura.saldo_pendiente,
            "saldo_a_favor": cliente.saldo_a_favor,
            "reactivado": reactivado,
        }

    # ==========================================
    # HELPERS
    # ==========================================

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
        promesa, factura, cliente, politica = (
            await FinanceService(self.db).registrar_promesa(
                factura_id,
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
