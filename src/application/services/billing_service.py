from datetime import date, datetime, timedelta
from typing import Optional, List
from dateutil.relativedelta import relativedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, desc, func, extract
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
        mes_actual_str = hoy.strftime("%B %Y").capitalize()
        notificador = NotificationService(self.db) # 👈 Instancia única

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
            
            try:
                fecha_vencimiento = date(hoy.year, hoy.month, plantilla.dia_pago)
            except ValueError:
                fecha_vencimiento = hoy + relativedelta(day=31)

            dias_antes = plantilla.dias_antes_emision or 0
            fecha_generacion = fecha_vencimiento - timedelta(days=dias_antes)
            
            if not dia_objetivo and hoy < fecha_generacion:
                continue

            # Evitar duplicados
            stmt_dup = select(FacturaModel).where(and_(
                FacturaModel.cliente_id == cliente.id,
                extract('month', FacturaModel.fecha_vencimiento) == fecha_vencimiento.month,
                extract('year', FacturaModel.fecha_vencimiento) == fecha_vencimiento.year
            ))
            if (await self.db.execute(stmt_dup)).scalars().first():
                continue 

            # Creación
            total = plan.precio + (plan.precio * plantilla.impuesto / 100)

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

            # 👇 NOTIFICACIÓN DE COBRO (Simple y limpia) 👇
            if cliente.telefono:
                # Solo pasamos lo que no es "fijo" del cliente (monto y folio)
                exito = await notificador.notificar(
                    "nueva_factura", 
                    cliente.id, 
                    variables_extra={"monto": f"${total}", "folio": str(nueva_factura.id)}
                )
                if exito: reporte["mensajes_enviados"] += 1

        await self.db.commit()
        return reporte

    # ==========================================
    # 2. MOTOR DE CORTES AUTOMÁTICOS
    # ==========================================
    async def procesar_cortes_automaticos(self):
        hoy = date.today()
        notificador = NotificationService(self.db)

        stmt = select(FacturaModel).options(
            joinedload(FacturaModel.cliente).joinedload(ClienteModel.router)
        ).where(
            FacturaModel.estado == 'pendiente',
            FacturaModel.fecha_limite_corte < hoy,
            FacturaModel.es_promesa_activa == False 
        )
        
        facturas_vencidas = (await self.db.execute(stmt)).scalars().all()
        reporte = {"clientes_suspendidos": 0, "errores": 0}

        for factura in facturas_vencidas:
            cliente = factura.cliente
            if cliente.estado == 'activo':
                cliente.estado = 'suspendido'
                factura.estado = 'vencida' 
                
                try:
                    mk = MikroTikService(cliente.router.ip_vpn, cliente.router.user_api, cliente.router.pass_api, cliente.router.port_api)
                    mk.gestionar_corte_cliente(cliente.ip_asignada, suspender=True)
                    if cliente.user_pppoe: mk.desconectar_cliente_activo(cliente.user_pppoe)
                    
                    reporte["clientes_suspendidos"] += 1
                    # 👇 AVISO DE CORTE 👇
                    await notificador.notificar("aviso_corte", cliente.id)
                except Exception as e:
                    reporte["errores"] += 1

        await self.db.commit()
        return reporte

    # ==========================================
    # 3. REGISTRO DE PAGOS (CON PDF ADJUNTO)
    # ==========================================
    async def registrar_pago_completo(self, factura_id: int, usuario_operador: UsuarioModel, metodo_pago: str, monto: float, referencia: str = None):
        factura = await self.db.get(FacturaModel, factura_id)
        if not factura: raise ValueError("Factura no encontrada")
        
        # Necesitamos cargar el cliente con sus planes para el PDF
        stmt_c = select(ClienteModel).options(
            selectinload(ClienteModel.plan),
            selectinload(ClienteModel.plantilla),
            joinedload(ClienteModel.router)
        ).where(ClienteModel.id == factura.cliente_id)
        cliente = (await self.db.execute(stmt_c)).scalar_one()
        
        factura.estado = "pagada"
        factura.saldo_pendiente = 0
        factura.fecha_pago_real = datetime.now()
        factura.es_promesa_activa = False 
        
        nuevo_pago = PagoModel(
            cliente_id=cliente.id, factura_id=factura.id, 
            usuario_id=usuario_operador.id, monto_total=monto, 
            metodo_pago=metodo_pago, referencia=referencia,
            fecha_pago=datetime.now()
        )
        self.db.add(nuevo_pago)
        
        reactivado = False
        if cliente.estado == 'suspendido':
            cliente.estado = 'activo'
            reactivado = await self._reactivar_en_mikrotik(cliente)

        await self.db.commit() 

        # 👇 🚀 LOGICA DE COMPROBANTE WHATSAPP UNIFICADA 👇
        if cliente.telefono:
            try:
                # 1. Generar la fecha del próximo vencimiento para el PDF
                prox_venc = (factura.fecha_vencimiento + relativedelta(months=1)).strftime("%d/%m/%Y")
                
                # 2. Generar el archivo PDF físico
                ruta_pdf = await generar_recibo_pdf(
                    nombre_cliente=cliente.nombre,
                    monto=monto,
                    concepto=f"MENSUALIDAD INTERNET - {factura.plan_snapshot}",
                    fecha_pago=nuevo_pago.fecha_pago,
                    folio=factura.id,
                    nueva_fecha_vencimiento=prox_venc,
                    telefono_cliente=cliente.telefono,
                    metodo_pago=metodo_pago
                )

                # 3. Enviar mensaje de pago_recibido ADJUNTANDO el PDF
                notificador = NotificationService(self.db)
                await notificador.notificar(
                    tipo_evento="pago_recibido", 
                    cliente_id=cliente.id,
                    variables_extra={"monto_pagado": f"${monto}", "referencia": referencia or "N/A"},
                    ruta_pdf=ruta_pdf # 👈 Aquí ocurre la magia del adjunto
                )
                
            except Exception as e:
                print(f"⚠️ Error al notificar pago con PDF: {e}")

        return {"status": "ok", "reactivado": reactivado}

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