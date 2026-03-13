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

# Servicios
from src.infrastructure.mikrotik_service import MikroTikService
from src.application.services.notification_service import NotificationService

class BillingService:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ==========================================
    # 1. GENERACIÓN MASIVA INTELIGENTE
    # ==========================================
    async def generar_emision_masiva(self, dia_objetivo: int = None):
        """
        Genera facturas basándose en la configuración de 'dias_antes_emision'.
        """
        hoy = date.today()
        mes_actual_str = hoy.strftime("%B %Y").capitalize()
        notificador = NotificationService(self.db)

        # 1. Traer TODOS los clientes activos con sus plantillas y planes
        stmt = select(ClienteModel).options(
            selectinload(ClienteModel.plantilla),
            selectinload(ClienteModel.plan)
        ).where(
            ClienteModel.estado.in_(['activo', 'suspendido']),
            ClienteModel.plantilla_id.isnot(None),
            ClienteModel.plan_id.isnot(None)
        )
        
        # SI ES MODO MANUAL (Filtramos por grupo de pago, ej: 15)
        if dia_objetivo:
            print(f"🔧 MODO MANUAL: Forzando generación para clientes con día de pago {dia_objetivo}")
            stmt = stmt.join(PlantillaFacturacionModel).where(
                PlantillaFacturacionModel.dia_pago == dia_objetivo
            )
        
        result = await self.db.execute(stmt)
        clientes = result.scalars().all()
        
        nuevas_facturas = []
        reporte = {
            "total_procesados": 0, 
            "facturas_generadas": 0, 
            "mensajes_enviados": 0,
            "omitidos_por_fecha": 0,
            "omitidos_ya_existentes": 0
        }

        for cliente in clientes:
            plantilla = cliente.plantilla
            plan = cliente.plan
            if not plantilla or not plan: continue
            
            reporte["total_procesados"] += 1

            # --- LÓGICA DE CALENDARIO ---
            dia_pago = plantilla.dia_pago
            
            # 1. Calcular Fecha de Vencimiento (Día X de este mes)
            try:
                fecha_vencimiento = date(hoy.year, hoy.month, dia_pago)
            except ValueError:
                fecha_vencimiento = hoy + relativedelta(day=31)

            # 2. Calcular Fecha de Generación (El día que debe crearse)
            dias_antes = plantilla.dias_antes_emision if plantilla.dias_antes_emision else 0
            fecha_generacion = fecha_vencimiento - timedelta(days=dias_antes)
            
            # --- DECISIÓN: ¿GENERAMOS LA FACTURA? ---
            debe_generar = False
            
            if dia_objetivo:
                debe_generar = True
            else:
                if hoy == fecha_generacion:
                    debe_generar = True
                    print(f"✅ Hoy {hoy} toca facturar a {cliente.nombre} (Vence el {fecha_vencimiento})")
                else:
                    reporte["omitidos_por_fecha"] += 1

            if not debe_generar:
                continue

            # --- EVITAR DUPLICADOS ---
            stmt_dup = select(FacturaModel).where(and_(
                FacturaModel.cliente_id == cliente.id,
                extract('month', FacturaModel.fecha_vencimiento) == fecha_vencimiento.month,
                extract('year', FacturaModel.fecha_vencimiento) == fecha_vencimiento.year
            ))
            res_dup = await self.db.execute(stmt_dup)
            if res_dup.scalars().first():
                reporte["omitidos_ya_existentes"] += 1
                continue 

            # --- CREACIÓN ---
            dias_tolerancia = plantilla.dias_tolerancia if plantilla.dias_tolerancia else 0
            fecha_corte = fecha_vencimiento + timedelta(days=dias_tolerancia)

            subtotal = plan.precio
            impuesto_val = (subtotal * plantilla.impuesto) / 100
            total = subtotal + impuesto_val

            nueva_factura = FacturaModel(
                cliente_id=cliente.id,
                plan_snapshot=plan.nombre,
                detalles=f"Servicio Internet - {plan.nombre}",
                monto=subtotal,
                impuesto=impuesto_val,
                total=total,
                saldo_pendiente=total,
                estado='pendiente',
                fecha_emision=hoy,              
                fecha_vencimiento=fecha_vencimiento, 
                fecha_limite_corte=fecha_corte,      
                mes_correspondiente=mes_actual_str
            )
            
            self.db.add(nueva_factura)
            await self.db.flush()
            nuevas_facturas.append(nueva_factura)
            reporte["facturas_generadas"] += 1

            # --- ENVÍO WHATSAPP ---
            if cliente.telefono:
                try:
                    vars_msg = {
                        "nombre_cliente": cliente.nombre,
                        "monto": f"${total}",
                        "fecha_vencimiento": fecha_vencimiento.strftime("%d/%m/%Y"),
                        "folio": str(nueva_factura.id)
                    }
                    await notificador.notificar_evento("nueva_factura", cliente.id, vars_msg)
                    reporte["mensajes_enviados"] += 1
                except Exception as e:
                    print(f"⚠️ Error enviando WhatsApp a {cliente.nombre}: {e}")

        if nuevas_facturas:
            await self.db.commit()
            
        return reporte

    # ==========================================
    # 2. MOTOR DE CORTES AUTOMÁTICOS
    # ==========================================
    async def procesar_cortes_automaticos(self):
        """
        Busca todas las facturas pendientes cuya fecha límite de corte ya pasó
        y suspende a los clientes automáticamente en BD y MikroTik.
        """
        hoy = date.today()
        notificador = NotificationService(self.db)
        
        # Buscar facturas vencidas (límite superado)
        stmt = select(FacturaModel).options(
            joinedload(FacturaModel.cliente).joinedload(ClienteModel.router)
        ).where(
            FacturaModel.estado == 'pendiente',
            FacturaModel.fecha_limite_corte < hoy,
            FacturaModel.es_promesa_activa == False 
        )
        
        result = await self.db.execute(stmt)
        facturas_vencidas = result.scalars().all()

        reporte = {"clientes_suspendidos": 0, "errores": 0}

        for factura in facturas_vencidas:
            cliente = factura.cliente
            
            # Solo cortamos si el cliente sigue activo
            if cliente.estado == 'activo':
                cliente.estado = 'suspendido'
                factura.estado = 'vencida' 
                
                if cliente.router:
                    try:
                        mk = MikroTikService(
                            cliente.router.ip_vpn, 
                            cliente.router.user_api, 
                            cliente.router.pass_api, 
                            cliente.router.port_api
                        )
                        # 1. Agregar al Firewall (Address-List)
                        mk.gestionar_corte_cliente(cliente.ip_asignada, suspender=True)
                        
                        # 2. Desconectar (Kick) para bloqueo inmediato
                        if cliente.user_pppoe:
                            mk.desconectar_cliente_activo(cliente.user_pppoe)
                            
                        reporte["clientes_suspendidos"] += 1
                        
                        # 3. WhatsApp de Suspensión
                        if cliente.telefono:
                            vars_msg = {
                                "nombre_cliente": cliente.nombre,
                                "monto": f"${factura.total}",
                            }
                            await notificador.notificar_evento("aviso_corte", cliente.id, vars_msg)
                            
                    except Exception as e:
                        print(f"⚠️ Error al cortar a {cliente.nombre} en MK: {e}")
                        reporte["errores"] += 1

        if reporte["clientes_suspendidos"] > 0:
            await self.db.commit()
            print(f"🔴 CORTES EJECUTADOS: {reporte['clientes_suspendidos']} clientes suspendidos hoy {hoy}.")
            
        return reporte

    # ==========================================
    # 3. PAGOS Y LISTADOS
    # ==========================================
    async def registrar_pago_completo(self, factura_id: int, usuario_operador: UsuarioModel, metodo_pago: str, monto: float, referencia: str = None):
        factura = await self.db.get(FacturaModel, factura_id)
        if not factura: raise ValueError("Factura no encontrada")
        cliente = await self.db.get(ClienteModel, factura.cliente_id)
        
        factura.estado = "pagada"
        factura.saldo_pendiente = 0
        factura.fecha_pago_real = datetime.now()
        factura.es_promesa_activa = False 
        
        nuevo_pago = PagoModel(
            cliente_id=cliente.id,
            factura_id=factura.id, 
            usuario_id=usuario_operador.id,
            monto_total=monto, 
            metodo_pago=metodo_pago,
            referencia=referencia,
            fecha_pago=datetime.now()
        )
        self.db.add(nuevo_pago)
        
        reactivado = False
        if cliente.estado == 'suspendido':
            cliente.estado = 'activo'
            reactivado = await self._reactivar_en_mikrotik(cliente)

        await self.db.commit() 
        return {"status": "ok", "reactivado": reactivado}

    async def listar_facturas_por_permisos(self, usuario_id_solicitante: int, cliente_id: Optional[int] = None, router_id: Optional[int] = None):
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

    # ==========================================
    # HELPERS
    # ==========================================
    async def _reactivar_en_mikrotik(self, cliente):
        """Reactivación fluida para redes FTTH/PPPoE (Sin caída de enlace)."""
        if not cliente.router_id: return False
        
        try:
            router = await self.db.get(RouterModel, cliente.router_id)
            if not router or not router.is_active: return False
            
            mk = MikroTikService(router.ip_vpn, router.user_api, router.pass_api, router.port_api)
            
            # 1. Quitar de la lista de morosos (Firewall)
            # Al sacarlo de la lista, el MikroTik permite el tráfico inmediatamente
            # sin desconectar el PPPoE. ¡El cliente ni siente la transición!
            mk.gestionar_corte_cliente(cliente.ip_asignada, suspender=False)
            
            return True
            
        except Exception as e:
            print(f"⚠️ Error reactivando en MK para {cliente.nombre}: {e}")
            return False