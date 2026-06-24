from datetime import date, datetime, timedelta
from typing import Optional, List
from dateutil.relativedelta import relativedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_, desc, func, extract
from sqlalchemy.orm import joinedload, selectinload

# Modelos
from src.infrastructure.models import (
    ClienteModel, FacturaModel, PagoModel, 
    UsuarioModel, PlantillaFacturacionModel, PlanModel, RouterModel
)

# Servicios e Helpers
from src.infrastructure.mikrotik_service import MikroTikService
# 👇 IMPORTAMOS EL NUEVO SERVICIO UNIFICADO 👇
from src.application.services.notification_service import NotificationService
from src.application.helpers.pdf_generator import generar_recibo_pdf

class BillingService:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ==========================================
    # 1. GENERACIÓN MASIVA INTELIGENTE
    # ==========================================
    async def generar_emision_masiva(self, dia_objetivo: int = None):
        hoy = date.today()
        notificador = NotificationService(self.db) 

        stmt = select(ClienteModel).options(
            selectinload(ClienteModel.plantilla),
            selectinload(ClienteModel.plan)
        ).where(
            ClienteModel.estado.in_(['activo', 'suspendido']),
            ClienteModel.plantilla_id.isnot(None),
            ClienteModel.plan_id.isnot(None)
        )
        
        if dia_objetivo:
            stmt = stmt.join(PlantillaFacturacionModel).where(
                PlantillaFacturacionModel.dia_pago == dia_objetivo
            )
        
        result = await self.db.execute(stmt)
        clientes = result.scalars().all()
        
        reporte = {"total_procesados": 0, "facturas_generadas": 0, "mensajes_enviados": 0}

        for cliente in clientes:
            plantilla = cliente.plantilla
            plan = cliente.plan
            reporte["total_procesados"] += 1
            
            # 🔥 LÓGICA CORREGIDA DE FECHAS (EL BUG ESTABA AQUÍ) 🔥
            try:
                venc_este_mes = date(hoy.year, hoy.month, plantilla.dia_pago)
            except ValueError:
                # Si el mes no tiene día 31, lo ajusta al último día del mes (ej. Febrero 28)
                ultimo_dia_mes = (date(hoy.year, hoy.month, 1) + relativedelta(months=1, days=-1)).day
                venc_este_mes = date(hoy.year, hoy.month, min(plantilla.dia_pago, ultimo_dia_mes))

            # Si hoy ya pasó la fecha de pago de este mes (ej. hoy es 26 y el pago es el 1),
            # significa que estamos calculando la factura del PRÓXIMO mes (Junio).
            if hoy > venc_este_mes:
                prox_mes_date = hoy + relativedelta(months=1)
                ultimo_dia_prox = (date(prox_mes_date.year, prox_mes_date.month, 1) + relativedelta(months=1, days=-1)).day
                fecha_vencimiento = date(prox_mes_date.year, prox_mes_date.month, min(plantilla.dia_pago, ultimo_dia_prox))
            else:
                fecha_vencimiento = venc_este_mes

            dias_antes = plantilla.dias_antes_emision or 0
            fecha_generacion = fecha_vencimiento - timedelta(days=dias_antes)
            
            # 1. Si no es el día, saltamos (A menos que el cajero lo force manualmente)
            if not dia_objetivo and hoy < fecha_generacion:
                continue

            # 2. Evitar duplicados (Buscando por el mes/año correcto de vencimiento)
            stmt_dup = select(FacturaModel).where(and_(
                FacturaModel.cliente_id == cliente.id,
                extract('month', FacturaModel.fecha_vencimiento) == fecha_vencimiento.month,
                extract('year', FacturaModel.fecha_vencimiento) == fecha_vencimiento.year
            ))
            if (await self.db.execute(stmt_dup)).scalars().first():
                continue 

            # 3. Creación
            total = plan.precio + (plan.precio * plantilla.impuesto / 100)
            
            # 🚀 EXTRA: Ahora el recibo dice "Junio 2026", no "Mayo 2026"
            mes_actual_str = fecha_vencimiento.strftime("%B %Y").capitalize()

            nueva_factura = FacturaModel(
                cliente_id=cliente.id,
                plan_snapshot=plan.nombre,
                detalles=f"Servicio Internet - {plan.nombre}",
                monto=plan.precio, total=total, saldo_pendiente=total,
                estado='pendiente', fecha_emision=hoy,              
                fecha_vencimiento=fecha_vencimiento, 
                fecha_limite_corte=fecha_vencimiento + timedelta(days=plantilla.dias_tolerancia or 0),      
                mes_correspondiente=mes_actual_str
            )
            self.db.add(nueva_factura)
            await self.db.flush()
            reporte["facturas_generadas"] += 1

            # 👇 NOTIFICACIÓN DE COBRO 👇
            if cliente.telefono:
                exito = await notificador.notificar(
                    "nueva_factura", 
                    cliente.id, 
                    variables_extra={"monto": f"${total}", "folio": str(nueva_factura.id)}
                )
                if exito: reporte["mensajes_enviados"] += 1

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
        hoy = date.today()
        notificador = NotificationService(self.db)

        # 🔥 LA MAGIA: Usamos or_() para atrapar ambos casos
        stmt = select(FacturaModel).options(
            joinedload(FacturaModel.cliente).joinedload(ClienteModel.router)
        ).where(
            FacturaModel.estado == 'pendiente',
            or_(
                # CASO 1: Corte Normal (Llegó su fecha límite de corte y NO tiene prórroga)
                and_(
                    FacturaModel.fecha_limite_corte <= hoy,
                    FacturaModel.es_promesa_activa == False
                ),
                # CASO 2: Promesa Rota (Tiene prórroga, pero la fecha de promesa ya pasó)
                # NOTA: Usamos "< hoy" para cortarlo al día SIGUIENTE de su promesa
                and_(
                    FacturaModel.es_promesa_activa == True,
                    FacturaModel.fecha_promesa_pago < hoy 
                )
            )
        )
        
        facturas_vencidas = (await self.db.execute(stmt)).scalars().all()
        reporte = {"clientes_suspendidos": 0, "promesas_rotas": 0, "errores": 0}

        for factura in facturas_vencidas:
            cliente = factura.cliente
            if cliente.estado == 'activo':
                cliente.estado = 'suspendido'
                factura.estado = 'vencida'
                
                # Si lo estamos cortando por romper la promesa, la desactivamos
                if factura.es_promesa_activa:
                    factura.es_promesa_activa = False
                    reporte["promesas_rotas"] += 1
                
                try:
                    mk = MikroTikService(cliente.router.ip_vpn, cliente.router.user_api, cliente.router.pass_api, cliente.router.port_api)
                    mk.gestionar_corte_cliente(cliente.ip_asignada, suspender=True)
                    if cliente.user_pppoe: mk.desconectar_cliente_activo(cliente.user_pppoe)
                    
                    reporte["clientes_suspendidos"] += 1
                    
                    # 👇 AVISO DE CORTE 👇
                    await notificador.notificar("aviso_corte", cliente.id)
                except Exception as e:
                    reporte["errores"] += 1
                    print(f"⚠️ Error al cortar en MikroTik al cliente {cliente.nombre}: {e}")

        await self.db.commit()
        return reporte

    # ==========================================
    # 3. REGISTRO DE PAGOS (CON PDF ADJUNTO)
    # ==========================================
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
        if pago_completado and estado_previo == 'suspendido':
            cliente.estado = 'activo'
            reactivado = await self._reactivar_en_mikrotik(cliente)

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