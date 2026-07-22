from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc, case
from sqlalchemy.orm import joinedload
from datetime import datetime
import pytz
import psutil

# Modelos y Servicios (Ya no importamos MikroTikService aquí para el home)
from src.infrastructure.models import ClienteModel, PagoModel, RouterModel, FacturaModel

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
        
        inicio_dia_db = inicio_dia_mx.astimezone(pytz.utc)
        inicio_mes_db = inicio_mes_mx.astimezone(pytz.utc)

        # --- B. RESUMEN CLIENTES (Conteo Rápido) ---
        stmt_cli = select(
            func.count(ClienteModel.id).label("total"),
            func.sum(case((ClienteModel.estado == 'activo', 1), else_=0)).label("activos"),
            func.sum(case((ClienteModel.estado == 'suspendido', 1), else_=0)).label("suspendidos"),
            func.sum(case((ClienteModel.estado == 'retirado', 1), else_=0)).label("retirados")
        )
        res_cli = (await self.db.execute(stmt_cli)).one()

        # --- C. FINANZAS (Ingresos Reales) ---
        stmt_hoy = select(func.sum(PagoModel.monto_total)).where(PagoModel.fecha_pago >= inicio_dia_db)
        stmt_mes = select(func.sum(PagoModel.monto_total)).where(PagoModel.fecha_pago >= inicio_mes_db)
        
        cobrado_hoy = (await self.db.execute(stmt_hoy)).scalar() or 0.0
        cobrado_mes = (await self.db.execute(stmt_mes)).scalar() or 0.0

        # --- D. SERVIDOR (Recursos) ---
        vmem = psutil.virtual_memory()
        disk = psutil.disk_usage('/')

        return {
            "resumen_clientes": {
                "total_registrados": res_cli.total or 0,
                "online_activos": res_cli.activos or 0,
                "offline_cortados": res_cli.suspendidos or 0,
                "retirados": res_cli.retirados or 0
            },
            "finanzas": {
                "cobrado_hoy": float(cobrado_hoy),
                "cobrado_mes": float(cobrado_mes),
                "moneda": "MXN"
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
            select(FacturaModel.cliente_id)
            .join(ClienteModel, ClienteModel.id == FacturaModel.cliente_id)
            .where(
                FacturaModel.estado.in_(["pendiente", "vencida"]),
                FacturaModel.saldo_pendiente > 0,
                ClienteModel.estado != "eliminado",
            )
            .distinct()
        )
        ids_morosos = set((await self.db.execute(stmt_morosos)).scalars().all())

        stmt_clientes = (
            select(ClienteModel.id, ClienteModel.is_online)
            .where(ClienteModel.estado == "activo")
        )
        clientes_activos = (await self.db.execute(stmt_clientes)).all()

        navegando_ok = 0
        morosos_online = 0
        falla_tecnica = 0
        morosos_offline = 0

        for c_id, is_online in clientes_activos:
            es_moroso = c_id in ids_morosos

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

        total_online = navegando_ok + morosos_online
        total_offline = falla_tecnica + morosos_offline

        return {
            "metricas": {
                "total_clientes": len(clientes_activos),
                "navegando_ok": navegando_ok,
                "falla_tecnica": falla_tecnica,
                "morosos_online": morosos_online,
                "morosos_offline": morosos_offline,

                # Alias para compatibilidad con frontend/dashboard
                "online": total_online,
                "offline": total_offline,
                "total_online": total_online,
                "total_offline": total_offline,
            }
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