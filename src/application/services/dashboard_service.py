from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc, case, and_
from sqlalchemy.orm import joinedload
from datetime import datetime, timedelta
import pytz
import psutil

# Modelos y Servicios (Ya no importamos MikroTikService aquí para el home)
from src.infrastructure.models import (
    ClienteModel,
    PagoModel,
    RouterModel,
    FacturaModel,
    ServicioModel,
)

class DashboardService:
    def __init__(self, db: AsyncSession):
        self.db = db
        # Definimos la zona horaria para mostrar datos correctamente al usuario
        self.tz_mexico = pytz.timezone('America/Mexico_City')

    # ==========================================
    # 1. DATOS PARA /home (KPIs Principales)
    # ==========================================
    async def obtener_home_data(self):
        """
        Calcula KPIs Financieros, Estado de Clientes y Salud del Servidor.
        """
        # --- A. FECHAS (Cortes de Caja) ---
        ahora_mx = datetime.now(self.tz_mexico)
        inicio_dia_mx = ahora_mx.replace(hour=0, minute=0, second=0, microsecond=0)
        inicio_mes_mx = inicio_dia_mx.replace(day=1)
        fin_dia_mx = inicio_dia_mx + timedelta(days=1)
        if inicio_mes_mx.month == 12:
            fin_mes_mx = inicio_mes_mx.replace(
                year=inicio_mes_mx.year + 1,
                month=1,
            )
        else:
            fin_mes_mx = inicio_mes_mx.replace(
                month=inicio_mes_mx.month + 1,
            )
        
        inicio_dia_db = inicio_dia_mx.astimezone(pytz.utc)
        inicio_mes_db = inicio_mes_mx.astimezone(pytz.utc)
        fin_dia_db = fin_dia_mx.astimezone(pytz.utc)
        fin_mes_db = fin_mes_mx.astimezone(pytz.utc)

        # --- B. RESUMEN CLIENTES (Conteo Rápido) ---
        estados_actuales = ("activo", "suspendido")
        stmt_cli = (
            select(
                func.count(
                    func.distinct(ClienteModel.id)
                ).label("total"),
                func.count(
                    func.distinct(
                        case(
                            (
                                ServicioModel.estado.in_(estados_actuales),
                                ClienteModel.id,
                            ),
                            else_=None,
                        )
                    )
                ).label("total_directorio"),
                func.sum(
                    case(
                        (ServicioModel.estado == "activo", 1),
                        else_=0,
                    )
                ).label("activos"),
                func.sum(
                    case(
                        (ServicioModel.estado == "suspendido", 1),
                        else_=0,
                    )
                ).label("suspendidos"),
                func.sum(
                    case(
                        (ServicioModel.estado.in_(estados_actuales), 1),
                        else_=0,
                    )
                ).label("servicios_actuales"),
                func.count(ServicioModel.id).label("total_servicios"),
                func.sum(
                    case(
                        (
                            ServicioModel.estado
                            == "pendiente_instalacion",
                            1,
                        ),
                        else_=0,
                    )
                ).label("pendientes_instalacion"),
                func.sum(
                    case(
                        (ServicioModel.estado == "cancelado", 1),
                        else_=0,
                    )
                ).label("cancelados"),
                func.sum(
                    case(
                        (ServicioModel.estado == "retirado", 1),
                        else_=0,
                    )
                ).label("retirados"),
                func.sum(
                    case(
                        (ServicioModel.estado == "eliminado", 1),
                        else_=0,
                    )
                ).label("eliminados"),
            )
            .select_from(ClienteModel)
            .outerjoin(
                ServicioModel,
                ServicioModel.cliente_id == ClienteModel.id,
            )
        )
        res_cli = (await self.db.execute(stmt_cli)).one()
        total_historico = int(res_cli.total or 0)
        total_directorio = int(res_cli.total_directorio or 0)
        contratos_activos = int(res_cli.activos or 0)
        contratos_suspendidos = int(res_cli.suspendidos or 0)
        servicios_actuales = int(res_cli.servicios_actuales or 0)
        total_servicios_historico = int(res_cli.total_servicios or 0)
        pendientes_instalacion = int(res_cli.pendientes_instalacion or 0)
        cancelados = int(res_cli.cancelados or 0)
        retirados = int(res_cli.retirados or 0)
        eliminados = int(res_cli.eliminados or 0)
        servicios_conocidos = (
            contratos_activos
            + contratos_suspendidos
            + pendientes_instalacion
            + cancelados
            + retirados
            + eliminados
        )
        otros_estados = max(
            total_servicios_historico - servicios_conocidos,
            0,
        )

        # --- C. FINANZAS (Ingresos Reales) ---
        def consulta_cobranza(desde, hasta):
            es_ingreso_real = PagoModel.metodo_pago != "saldo_favor"
            return select(
                func.coalesce(
                    func.sum(
                        case(
                            (es_ingreso_real, PagoModel.monto_total),
                            else_=0,
                        )
                    ),
                    0,
                ).label("monto_cobrado"),
                func.count(
                    case((es_ingreso_real, PagoModel.id), else_=None)
                ).label("pagos_recibidos"),
                func.count(
                    func.distinct(
                        case(
                            (es_ingreso_real, PagoModel.cliente_id),
                            else_=None,
                        )
                    )
                ).label("clientes_cobrados"),
            ).where(
                PagoModel.fecha_pago >= desde,
                PagoModel.fecha_pago < hasta,
                PagoModel.estado == "aplicado",
                ServicioModel.estado.in_(estados_actuales),
            ).join(
                ClienteModel,
                ClienteModel.id == PagoModel.cliente_id,
            ).join(
                FacturaModel,
                FacturaModel.id == PagoModel.factura_id,
            ).join(
                ServicioModel,
                ServicioModel.id == FacturaModel.servicio_id,
            )

        cobranza_hoy = (
            await self.db.execute(
                consulta_cobranza(inicio_dia_db, fin_dia_db)
            )
        ).one()
        cobranza_mes = (
            await self.db.execute(
                consulta_cobranza(inicio_mes_db, fin_mes_db)
            )
        ).one()

        # --- D. FACTURACIÓN DEL MES SOBRE LOS MISMOS CLIENTES ACTUALES ---
        factura_valida = FacturaModel.estado != "anulada"
        factura_pendiente = and_(
            FacturaModel.estado.in_(["pendiente", "vencida"]),
            FacturaModel.saldo_pendiente > 0,
        )
        stmt_facturacion = (
            select(
                func.count(
                    case((factura_valida, FacturaModel.id), else_=None)
                ).label("facturas_emitidas"),
                func.count(
                    case(
                        (FacturaModel.estado == "pagada", FacturaModel.id),
                        else_=None,
                    )
                ).label("facturas_pagadas"),
                func.count(
                    case(
                        (FacturaModel.estado == "pendiente", FacturaModel.id),
                        else_=None,
                    )
                ).label("facturas_pendientes"),
                func.count(
                    case(
                        (FacturaModel.estado == "vencida", FacturaModel.id),
                        else_=None,
                    )
                ).label("facturas_vencidas"),
                func.count(
                    case(
                        (FacturaModel.estado == "anulada", FacturaModel.id),
                        else_=None,
                    )
                ).label("facturas_anuladas"),
                func.count(
                    func.distinct(
                        case(
                            (factura_valida, FacturaModel.cliente_id),
                            else_=None,
                        )
                    )
                ).label("clientes_facturados"),
                func.count(
                    func.distinct(
                        case(
                            (factura_valida, FacturaModel.servicio_id),
                            else_=None,
                        )
                    )
                ).label("servicios_facturados"),
                func.count(
                    func.distinct(
                        case(
                            (factura_pendiente, FacturaModel.cliente_id),
                            else_=None,
                        )
                    )
                ).label("clientes_con_saldo_pendiente"),
                func.coalesce(
                    func.sum(
                        case(
                            (factura_valida, FacturaModel.total),
                            else_=0,
                        )
                    ),
                    0,
                ).label("monto_facturado"),
                func.coalesce(
                    func.sum(
                        case(
                            (factura_pendiente, FacturaModel.saldo_pendiente),
                            else_=0,
                        )
                    ),
                    0,
                ).label("saldo_pendiente"),
            )
            .join(
                ClienteModel,
                ClienteModel.id == FacturaModel.cliente_id,
            )
            .join(
                ServicioModel,
                ServicioModel.id == FacturaModel.servicio_id,
            )
            .where(
                FacturaModel.fecha_emision >= inicio_mes_mx.date(),
                FacturaModel.fecha_emision < fin_mes_mx.date(),
                ServicioModel.estado.in_(estados_actuales),
            )
        )
        facturacion_mes = (
            await self.db.execute(stmt_facturacion)
        ).one()

        # --- E. SERVIDOR (Recursos) ---
        vmem = psutil.virtual_memory()
        disk = psutil.disk_usage('/')

        facturas_emitidas = int(facturacion_mes.facturas_emitidas or 0)
        facturas_pagadas = int(facturacion_mes.facturas_pagadas or 0)
        facturas_pendientes = int(
            facturacion_mes.facturas_pendientes or 0
        )
        facturas_vencidas = int(facturacion_mes.facturas_vencidas or 0)
        clientes_facturados = int(
            facturacion_mes.clientes_facturados or 0
        )
        servicios_facturados = int(
            facturacion_mes.servicios_facturados or 0
        )
        porcentaje_facturas_pagadas = (
            round((facturas_pagadas / facturas_emitidas) * 100, 1)
            if facturas_emitidas
            else 0.0
        )
        porcentaje_cobertura = (
            round((clientes_facturados / total_directorio) * 100, 1)
            if total_directorio
            else 0.0
        )

        return {
            "resumen_clientes": {
                # El directorio omite instalaciones pendientes. Se usa la misma
                # definición que /clientes/listado-completo-unificado para que
                # ambos módulos muestren el mismo total.
                "total_clientes": total_directorio,
                "total_registrados": total_directorio,
                "total_historico": total_historico,
                "contratos_activos": contratos_activos,
                "total_servicios_actuales": servicios_actuales,
                "total_servicios_historico": total_servicios_historico,
                "contratos_no_activos": contratos_suspendidos,
                "contratos_suspendidos": contratos_suspendidos,
                "pendientes_instalacion": pendientes_instalacion,
                "cancelados": cancelados,
                "retirados": retirados,
                "eliminados": eliminados,
                "otros_estados": otros_estados,

                # Alias heredados. Se conservan para no romper el frontend
                # desplegado; los nombres canónicos de arriba deben usarse en
                # implementaciones nuevas.
                "online_activos": contratos_activos,
                "offline_cortados": contratos_suspendidos,
            },
            "finanzas": {
                "cobrado_hoy": float(cobranza_hoy.monto_cobrado or 0),
                "clientes_cobrados_hoy": int(
                    cobranza_hoy.clientes_cobrados or 0
                ),
                "pagos_recibidos_hoy": int(
                    cobranza_hoy.pagos_recibidos or 0
                ),
                "cobrado_mes": float(cobranza_mes.monto_cobrado or 0),
                "clientes_cobrados_mes": int(
                    cobranza_mes.clientes_cobrados or 0
                ),
                "pagos_recibidos_mes": int(
                    cobranza_mes.pagos_recibidos or 0
                ),
                "moneda": "MXN",
                "criterio": (
                    "Solo pagos aplicados; no incluye anulados "
                    "ni saldo a favor"
                ),
            },
            "facturacion": {
                "periodo_desde": inicio_mes_mx.date(),
                "periodo_hasta": (
                    fin_mes_mx - timedelta(days=1)
                ).date(),
                "clientes_actuales": total_directorio,
                "clientes_facturados": clientes_facturados,
                "servicios_actuales": servicios_actuales,
                "servicios_facturados": servicios_facturados,
                "servicios_sin_factura": max(
                    servicios_actuales - servicios_facturados,
                    0,
                ),
                "clientes_sin_factura": max(
                    total_directorio - clientes_facturados,
                    0,
                ),
                "clientes_cobrados": int(
                    cobranza_mes.clientes_cobrados or 0
                ),
                "clientes_con_saldo_pendiente": int(
                    facturacion_mes.clientes_con_saldo_pendiente or 0
                ),
                "facturas_emitidas": facturas_emitidas,
                "facturas_pagadas": facturas_pagadas,
                "facturas_pendientes": facturas_pendientes,
                "facturas_vencidas": facturas_vencidas,
                "facturas_anuladas": int(
                    facturacion_mes.facturas_anuladas or 0
                ),
                "monto_facturado": float(
                    facturacion_mes.monto_facturado or 0
                ),
                "monto_cobrado": float(
                    cobranza_mes.monto_cobrado or 0
                ),
                "saldo_pendiente": float(
                    facturacion_mes.saldo_pendiente or 0
                ),
                "porcentaje_cobertura": porcentaje_cobertura,
                "porcentaje_facturas_pagadas": (
                    porcentaje_facturas_pagadas
                ),

                # Contrato compatible con la tarjeta actual.
                "total": facturas_emitidas,
                "pagadas": facturas_pagadas,
                "pendientes": (
                    facturas_pendientes + facturas_vencidas
                ),
                "porcentaje": porcentaje_facturas_pagadas,
            },
            "servidor": {
                "cpu_percent": psutil.cpu_percent(interval=None),
                "ram_total_gb": round(vmem.total / (1024**3), 1),
                "ram_usada_percent": vmem.percent,
                "disco_libre_percent": 100 - disk.percent
            },
            "ultimos_pagos": await self._obtener_ultimos_pagos()
        }

    # ==========================================
    # 2. MÉTRICAS DE RED (Tarjetas Online/Offline)
    # ==========================================
    async def obtener_metricas_red(self):
        """
        Métricas de red usando clientes.is_online.

        Separa correctamente:
        - clientes activos al día y online
        - clientes activos al día pero offline
        - clientes morosos online
        - clientes morosos offline
        """
        stmt_morosos = (
            select(FacturaModel.servicio_id)
            .join(
                ServicioModel,
                ServicioModel.id == FacturaModel.servicio_id,
            )
            .where(
                FacturaModel.estado.in_(["pendiente", "vencida"]),
                FacturaModel.saldo_pendiente > 0,
                ServicioModel.estado == "activo",
            )
            .distinct()
        )
        ids_morosos = set((await self.db.execute(stmt_morosos)).scalars().all())

        stmt_clientes = (
            select(
                ServicioModel.id,
                ServicioModel.cliente_id,
                ServicioModel.estado,
                ServicioModel.is_online,
            )
            .where(
                ServicioModel.estado.in_(["activo", "suspendido"])
            )
        )
        clientes_directorio = (await self.db.execute(stmt_clientes)).all()

        navegando_ok = 0
        morosos_online = 0
        falla_tecnica = 0
        morosos_offline = 0
        total_online_directorio = 0
        total_offline_directorio = 0
        no_activos_online = 0
        no_activos_offline = 0
        suspendidos_online = 0
        suspendidos_offline = 0

        clientes_actuales_ids = set()
        for servicio_id, cliente_id, estado, is_online in clientes_directorio:
            clientes_actuales_ids.add(cliente_id)
            if is_online:
                total_online_directorio += 1
            else:
                total_offline_directorio += 1

            if estado != "activo":
                if is_online:
                    no_activos_online += 1
                else:
                    no_activos_offline += 1

                if estado == "suspendido":
                    if is_online:
                        suspendidos_online += 1
                    else:
                        suspendidos_offline += 1
                continue

            es_moroso = servicio_id in ids_morosos

            if es_moroso:
                if is_online:
                    morosos_online += 1
                else:
                    morosos_offline += 1
            else:
                if is_online:
                    navegando_ok += 1
                else:
                    falla_tecnica += 1

        activos_online = navegando_ok + morosos_online
        activos_offline = falla_tecnica + morosos_offline
        total_contratos_activos = activos_online + activos_offline
        total_servicios_directorio = len(clientes_directorio)
        total_clientes_directorio = len(clientes_actuales_ids)
        total_no_activos = no_activos_online + no_activos_offline
        total_suspendidos = suspendidos_online + suspendidos_offline

        return {
            "metricas": {
                # Estado técnico de todo el directorio de clientes.
                "total_clientes_directorio": total_clientes_directorio,
                "total_servicios": total_servicios_directorio,
                "clientes_online_mikrotik": total_online_directorio,
                "clientes_offline_mikrotik": total_offline_directorio,

                # Estado técnico limitado a contratos activos.
                "total_contratos_activos": total_contratos_activos,
                "contratos_activos_online": activos_online,
                "clientes_activos_sin_sesion": activos_offline,
                "contratos_no_activos": total_no_activos,
                "total_suspendidos": total_suspendidos,
                "no_activos_online": no_activos_online,
                "no_activos_sin_sesion": no_activos_offline,
                "suspendidos_online": suspendidos_online,
                "suspendidos_sin_sesion": suspendidos_offline,
                "activos_al_corriente_offline": falla_tecnica,
                "navegando_ok": navegando_ok,
                "falla_tecnica": falla_tecnica,
                "morosos_online": morosos_online,
                "morosos_offline": morosos_offline,

                # Alias para compatibilidad con frontend/dashboard
                "total_clientes": total_clientes_directorio,
                "online": total_online_directorio,
                "offline": total_offline_directorio,
                "total_online": total_online_directorio,
                "total_offline": total_offline_directorio,
            },
            "conciliacion": {
                "total_clientes_directorio": total_clientes_directorio,
                "total_servicios": total_servicios_directorio,
                "online_mikrotik": total_online_directorio,
                "sin_sesion": total_offline_directorio,
                "contratos_activos": total_contratos_activos,
                "activos_online_mikrotik": activos_online,
                "activos_sin_sesion": activos_offline,
                "contratos_no_activos": total_no_activos,
                "no_activos_online": no_activos_online,
                "no_activos_sin_sesion": no_activos_offline,
                "cuadra_directorio": (
                    total_online_directorio + total_offline_directorio
                    == total_servicios_directorio
                ),
                "cuadra_contratos": (
                    activos_online + activos_offline
                    == total_contratos_activos
                ),
                "formula_directorio": (
                    "total_servicios = online_mikrotik "
                    "+ sin_sesion"
                ),
                "formula_contratos": (
                    "contratos_activos = activos_online_mikrotik "
                    "+ activos_sin_sesion"
                ),
            },
        }

    # ==========================================
    # 3. TABLA COLOREADA (Semáforo de Clientes)
    # ==========================================
    async def obtener_tabla_coloreada(self):
        """
        Retorna el estado técnico exacto leyendo MySQL.
        ⚡ CARGA EN MILISEGUNDOS y compatible con el Front ⚡
        """
        # Traemos ID, IP, Estado y el is_online que guardó el cronjob
        stmt = select(ClienteModel.id, ClienteModel.ip_asignada, ClienteModel.estado, ClienteModel.is_online)
        clientes_db = (await self.db.execute(stmt)).all()
        
        mapa_colores = {}
        
        for c_id, ip, estado, is_online in clientes_db:
            # --- LÓGICA DE COLORES ---
            if is_online:
                estado_tecnico = "ONLINE"
                estado_front = "online"  # 👈 Compatibilidad para el front viejo
                if estado == 'activo':
                    color = "green"   # Todo perfecto
                    diag = "Conexión estable"
                else:
                    color = "orange"  # Raro: Suspendido pero navegando
                    diag = "ALERTA: Suspendido con servicio activo"
            else:
                estado_tecnico = "OFFLINE"
                estado_front = "offline"  # 👈 Compatibilidad para el front viejo
                if estado == 'activo':
                    color = "red"     # ⚡ CORRECCIÓN: Volvemos a 'red' por si el front no reconoce 'rose'
                    diag = "Sin conexión al Router (Posible falla de luz o cable)"
                else:
                    color = "gray"    # Normal: Está cortado por falta de pago
                    diag = "Corte administrativo"

            # ⚡ FORZAMOS str(c_id) para garantizar que la llave viaje idéntica en el JSON
            mapa_colores[str(c_id)] = {
                "estado_tecnico": estado_tecnico,
                "color": color,
                "diagnostico_sistema": diag,
                # 👇 Salvavidas para el Frontend 👇
                "estado": estado_front,
                "is_online": is_online
            }
            
        return {"detalle_clientes": mapa_colores}

    # ==========================================
    # HELPERS PRIVADOS
    # ==========================================
    async def _obtener_ultimos_pagos(self):
        """Últimos 5 pagos para el widget de actividad reciente"""
        stmt = select(PagoModel).options(
            joinedload(PagoModel.cliente), 
            joinedload(PagoModel.usuario)
        ).join(
            ClienteModel,
            ClienteModel.id == PagoModel.cliente_id,
        ).join(
            FacturaModel,
            FacturaModel.id == PagoModel.factura_id,
        ).join(
            ServicioModel,
            ServicioModel.id == FacturaModel.servicio_id,
        ).where(
            PagoModel.estado == "aplicado",
            ServicioModel.estado.in_(["activo", "suspendido"]),
        ).order_by(desc(PagoModel.id)).limit(5)
        
        res = (await self.db.execute(stmt)).scalars().all()
        lista = []
        for p in res:
            try:
                fecha_mx = p.fecha_pago.astimezone(self.tz_mexico).strftime("%d/%m %H:%M")
            except:
                fecha_mx = p.fecha_pago.strftime("%d/%m %H:%M")

            lista.append({
                "cliente": p.cliente.nombre if p.cliente else "Cliente Eliminado",
                "monto": float(p.monto_total),
                "cobrador": p.usuario.nombre_completo if p.usuario else "Sistema",
                "fecha": fecha_mx
            })
        return lista
