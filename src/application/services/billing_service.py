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
            select(ClienteModel)
            .options(
                selectinload(ClienteModel.plantilla),
                selectinload(ClienteModel.plan),
                selectinload(ClienteModel.servicios),
            )
            .where(
                ClienteModel.estado.in_(["activo", "suspendido"]),
                ClienteModel.plantilla_id.isnot(None),
                ClienteModel.plan_id.isnot(None),
            )
        )

        if dia_objetivo:
            stmt = stmt.join(PlantillaFacturacionModel).where(
                PlantillaFacturacionModel.dia_pago == dia_objetivo
            )

        result = await self.db.execute(stmt)
        clientes = result.scalars().all()

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

        for cliente in clientes:
            reporte["total_procesados"] += 1
            plantilla = cliente.plantilla
            plan = cliente.plan
            servicio = next((item for item in cliente.servicios if item.estado in {"activo", "suspendido"}), None)

            if servicio is None:
                reporte["omitidos_sin_servicio"] += 1
                continue
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
                cliente.proxima_factura = periodo.siguiente_facturacion
                reporte["omitidos_factura_existente"] += 1
                continue

            if periodo.es_prorrateada:
                mes_actual_str = f"Prorrateo {periodo.periodo_desde.strftime('%d/%m/%Y')} - {periodo.periodo_hasta.strftime('%d/%m/%Y')}"
                detalles = f"Prorrateo Internet - {plan.nombre} ({periodo.dias_facturados} de {periodo.dias_periodo} días)"
            else:
                if periodo.periodo_desde.day == 1:
                    mes_actual_str = periodo.periodo_desde.strftime("%B %Y").capitalize()
                else:
                    mes_actual_str = f"Ciclo {periodo.periodo_desde.strftime('%d/%m/%Y')} - {periodo.periodo_hasta.strftime('%d/%m/%Y')}"
                detalles = f"Servicio Internet - {plan.nombre}"

            nueva_factura = FacturaModel(
                cliente_id=cliente.id,
                servicio_id=servicio.id,
                plan_snapshot=plan.nombre,
                detalles=detalles,
                monto=float(periodo.subtotal),
                impuesto=float(periodo.impuesto),
                total=float(periodo.total),
                saldo_pendiente=float(periodo.total),
                estado="pendiente",
                fecha_emision=hoy,
                fecha_vencimiento=fecha_vencimiento,
                fecha_limite_corte=fecha_vencimiento + timedelta(days=plantilla.dias_tolerancia or 0),
                mes_correspondiente=mes_actual_str,
                periodo_desde=periodo.periodo_desde,
                periodo_hasta=periodo.periodo_hasta,
                dias_facturados=periodo.dias_facturados,
                dias_periodo=periodo.dias_periodo,
                precio_mensual_snapshot=float(periodo.precio_mensual),
                precio_diario=float(periodo.precio_diario),
                es_prorrateada=periodo.es_prorrateada,
                tipo_facturacion_snapshot=tipo_snapshot,
                ciclo_facturacion_snapshot=ciclo_snapshot,
            )
            self.db.add(nueva_factura)
            await self.db.flush()

            servicio.proxima_facturacion = periodo.siguiente_facturacion
            cliente.proxima_factura = periodo.siguiente_facturacion
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
                        cliente_id=cliente.id
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
            .options(
                joinedload(FacturaModel.cliente).joinedload(
                    ClienteModel.router
                ),
                joinedload(FacturaModel.servicio),
            )
            .where(
                FacturaModel.estado.in_(["pendiente", "promesa", "vencida"]),
                FacturaModel.saldo_pendiente > 0,
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

        for factura in facturas:
            cliente = factura.cliente
            promesa_rota = bool(factura.es_promesa_activa)

            if cliente.estado == "suspendido":
                actualizados = (
                    await self._actualizar_estado_servicio_factura(
                        factura,
                        "suspendido",
                    )
                )
                factura.estado = "vencida"
                reporte["servicios_suspendidos"] += actualizados
                reporte["facturas_vencidas"] += 1
                reporte["omitidos_ya_suspendidos"] += 1

                if promesa_rota:
                    factura.es_promesa_activa = False
                    reporte["promesas_rotas"] += 1
                continue

            if not cliente.router or not cliente.ip_asignada:
                reporte["errores_mikrotik"] += 1
                continue

            try:
                mk = MikroTikService(
                    cliente.router.ip_vpn,
                    cliente.router.user_api,
                    cliente.router.pass_api,
                    cliente.router.port_api,
                )
                resultado = mk.gestionar_corte_cliente(
                    cliente.ip_asignada,
                    suspender=True,
                )

                if resultado is False:
                    raise RuntimeError(
                        "MikroTik no confirmó la suspensión."
                    )

                if cliente.user_pppoe:
                    mk.desconectar_cliente_activo(
                        cliente.user_pppoe
                    )
            except Exception as exc:
                reporte["errores_mikrotik"] += 1
                print(
                    "⚠️ Error al cortar al cliente "
                    f"{cliente.id}: {exc}"
                )
                continue

            cliente.estado = "suspendido"
            actualizados = (
                await self._actualizar_estado_servicio_factura(
                    factura,
                    "suspendido",
                )
            )
            factura.estado = "vencida"

            if promesa_rota:
                factura.es_promesa_activa = False
                reporte["promesas_rotas"] += 1

            reporte["clientes_suspendidos"] += 1
            reporte["servicios_suspendidos"] += actualizados
            reporte["facturas_vencidas"] += 1

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
                )

                if not enviado:
                    enviado = await notificador.notificar(
                        "aviso_corte",
                        cliente.id,
                        variables_extra=variables,
                    )

                if enviado:
                    reporte["notificaciones_enviadas"] += 1
            except Exception as exc:
                reporte["errores_notificacion"] += 1
                print(
                    "⚠️ Corte confirmado, pero falló WhatsApp "
                    f"para el cliente {cliente.id}: {exc}"
                )

        await self.db.commit()
        return reporte

    async def registrar_pago_completo(self, factura_id: int, usuario_operador: UsuarioModel, metodo_pago: str, monto: float, referencia: str = None):
        factura = await self.db.get(FacturaModel, factura_id)
        if not factura: raise ValueError("Factura no encontrada")
        if monto <= 0: raise ValueError("El monto debe ser mayor a cero.")
        
        # Necesitamos cargar el cliente con sus planes para el PDF
        stmt_c = select(ClienteModel).options(
            selectinload(ClienteModel.plan),
            selectinload(ClienteModel.plantilla),
            joinedload(ClienteModel.router)
        ).where(ClienteModel.id == factura.cliente_id)
        cliente = (await self.db.execute(stmt_c)).scalar_one()

        deuda_actual = factura.saldo_pendiente
        estado_previo = cliente.estado
        reactivado = False
        pago_completado = False # Bandera para saber si se envía el PDF o solo un aviso de abono

        # =======================================================
        # LÓGICA DE COBRO: PARCIAL, EXACTO O SOBRANTE
        # =======================================================
        if monto < deuda_actual:
            # A) ABONO PARCIAL
            factura.saldo_pendiente -= monto
            factura.estado = 'vencida' if factura.fecha_vencimiento < datetime.now().date() else 'pendiente'
            pago_completado = False

        elif monto == deuda_actual:
            # B) PAGO EXACTO
            factura.saldo_pendiente = 0.0
            factura.estado = 'pagada'
            factura.fecha_pago_real = datetime.now()
            factura.es_promesa_activa = False
            pago_completado = True

        else:
            # C) PAGA DE MÁS (Genera Saldo a Favor)
            sobrante = monto - deuda_actual
            
            factura.saldo_pendiente = 0.0
            factura.estado = 'pagada'
            factura.fecha_pago_real = datetime.now()
            factura.es_promesa_activa = False
            pago_completado = True

            # Acumulamos el dinero extra en el perfil del cliente
            if cliente.saldo_a_favor is None:
                cliente.saldo_a_favor = 0.0
            cliente.saldo_a_favor += sobrante

        # Registramos el movimiento en la caja (historial de pagos)
        nuevo_pago = PagoModel(
            cliente_id=cliente.id, factura_id=factura.id, 
            usuario_id=usuario_operador.id, monto_total=monto, # Se registra lo que dio el cliente físicamente
            metodo_pago=metodo_pago, referencia=referencia,
            fecha_pago=datetime.now()
        )
        self.db.add(nuevo_pago)
        
        # Reconexión automática SOLO si la factura quedó pagada por completo
        # FACTURACION_ISP_V2_SAFE_REACTIVATION
        if pago_completado and estado_previo == "suspendido":
            await self.db.flush()
            tiene_otra_deuda = (
                await self._cliente_tiene_deuda_pendiente(
                    cliente.id,
                    excluir_factura_id=factura.id,
                )
            )
            if not tiene_otra_deuda:
                reactivado = await self._reactivar_en_mikrotik(
                    cliente
                )
                if reactivado:
                    cliente.estado = "activo"
                    await self._actualizar_estado_servicio_factura(
                        factura,
                        "activo",
                    )

        await self.db.commit() 

        # 👇 🚀 LOGICA DE WHATSAPP 👇
        if cliente.telefono:
            try:
                notificador = NotificationService(self.db)
                
                if pago_completado:
                    # Si liquidó, se le manda su PDF
                    prox_venc = (factura.fecha_vencimiento + relativedelta(months=1)).strftime("%d/%m/%Y")
                    ruta_pdf = await generar_recibo_pdf(
                        nombre_cliente=cliente.nombre,
                        monto=monto, # Se refleja el pago total
                        concepto=f"MENSUALIDAD INTERNET - {factura.plan_snapshot}",
                        fecha_pago=nuevo_pago.fecha_pago,
                        folio=factura.id,
                        nueva_fecha_vencimiento=prox_venc,
                        telefono_cliente=cliente.telefono,
                        metodo_pago=metodo_pago
                    )
                    await notificador.notificar(
                        tipo_evento="pago_recibido", 
                        cliente_id=cliente.id,
                        variables_extra={"monto_pagado": f"${monto}", "referencia": referencia or "N/A"},
                        ruta_pdf=ruta_pdf
                    )
                else:
                    # Si solo abonó una parte, se manda este aviso sencillo sin PDF
                    await notificador.notificar(
                        tipo_evento="abono_recibido", # 👈 Aquí hace match con la BD
                        cliente_id=cliente.id,
                        variables_extra={
                            "monto_pagado": f"${monto}", 
                            "referencia": f"Abono parcial registrado. (Resta por pagar: ${factura.saldo_pendiente})"
                        }
                    )
                
            except Exception as e:
                print(f"⚠️ Error al notificar pago: {e}")

        return {"status": "ok", "factura_liquidada": pago_completado, "reactivado": reactivado}

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

    async def _reactivar_en_mikrotik(self, cliente):
        if not cliente.router_id: return False
        try:
            router = await self.db.get(RouterModel, cliente.router_id)
            if not router or not router.is_active: return False
            mk = MikroTikService(router.ip_vpn, router.user_api, router.pass_api, router.port_api)
            mk.gestionar_corte_cliente(cliente.ip_asignada, suspender=False)
            
            # 👇 NOTIFICACIÓN RECONEXIÓN 👇
            notificador = NotificationService(self.db)
            await notificador.notificar("reconexion", cliente.id)
            return True
        except Exception as e:
            print(f"⚠️ Error reactivando en MK: {e}")
            return False

    async def listar_facturas_por_permisos(self, usuario_id_solicitante: int, cliente_id: Optional[int] = None, router_id: Optional[int] = None):
        # ... (Tu código de permisos se mantiene igual) ...
        stmt_user = select(UsuarioModel).options(selectinload(UsuarioModel.routers_asignados)).where(UsuarioModel.id == usuario_id_solicitante)
        usuario = (await self.db.execute(stmt_user)).scalar_one()
        query = select(FacturaModel).join(ClienteModel).options(joinedload(FacturaModel.cliente).joinedload(ClienteModel.router))
        if cliente_id: query = query.where(FacturaModel.cliente_id == cliente_id)
        if router_id: query = query.where(ClienteModel.router_id == router_id)
        if usuario.rol != 'admin':
            ids_permitidos = [r.id for r in usuario.routers_asignados]
            if not ids_permitidos: return [] 
            query = query.where(ClienteModel.router_id.in_(ids_permitidos))
        query = query.order_by(FacturaModel.id.desc()).limit(200)
        return (await self.db.execute(query)).scalars().all()
