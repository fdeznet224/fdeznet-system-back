from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc, case
from sqlalchemy.orm import joinedload
from datetime import datetime
import pytz
import psutil

# Modelos y Servicios
from src.infrastructure.models import ClienteModel, PagoModel, RouterModel, FacturaModel
from src.infrastructure.mikrotik_service import MikroTikService

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
        
        # Convertimos a UTC porque en BD solemos guardar UTC (si usas DateTime(timezone=True))
        # Si tu BD guarda local time, puedes quitar el .astimezone(pytz.utc)
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
        Cruza información de MikroTik (Vivo) vs Base de Datos (Esperado)
        para detectar inconsistencias como "Falla Técnica" o "Moroso Navegando".
        """
        # 1. Escaneo Masivo (Quién está conectado realmente)
        usuarios_online = await self._escanear_red_masivo()
        
        # 2. Quiénes deben dinero (Morosos)
        stmt_morosos = select(FacturaModel.cliente_id).where(FacturaModel.estado == 'pendiente').distinct()
        ids_morosos = set((await self.db.execute(stmt_morosos)).scalars().all())

        # 3. Quiénes deberían tener servicio (Activos en BD)
        stmt_clientes = select(ClienteModel.id, ClienteModel.user_pppoe, ClienteModel.ip_asignada).where(ClienteModel.estado == 'activo')
        clientes_activos = (await self.db.execute(stmt_clientes)).all()

        navegando_ok = 0
        morosos_online = 0
        falla_tecnica = 0

        for c_id, c_user, c_ip in clientes_activos:
            # Buscamos por usuario o IP
            clave = c_user if c_user else c_ip
            esta_online = clave in usuarios_online
            es_moroso = c_id in ids_morosos

            if esta_online and not es_moroso:
                navegando_ok += 1
            elif esta_online and es_moroso:
                morosos_online += 1 # ⚠️ Alerta: Debe dinero pero sigue conectado
            elif not esta_online:
                falla_tecnica += 1  # ⚠️ Alerta: Está al día pero no conecta (¿Cable roto?)

        return {
            "metricas": {
                "total_clientes": len(clientes_activos),
                "navegando_ok": navegando_ok,
                "falla_tecnica": falla_tecnica,
                "morosos_online": morosos_online,
                "morosos_offline": len(ids_morosos) - morosos_online
            }
        }

    # ==========================================
    # 3. TABLA COLOREADA (Semáforo de Clientes)
    # ==========================================
    async def obtener_tabla_coloreada(self):
        """
        Retorna el estado técnico exacto para pintar la tabla de clientes.
        """
        usuarios_online = await self._escanear_red_masivo()
        
        # Traemos ID e IP para ser ligeros
        stmt = select(ClienteModel.ip_asignada, ClienteModel.user_pppoe, ClienteModel.estado)
        clientes_db = (await self.db.execute(stmt)).all()
        
        detalle = []
        for ip, user, estado in clientes_db:
            clave = user if user else ip
            esta_online = clave in usuarios_online

            # --- LÓGICA DE COLORES ---
            if esta_online:
                estado_tecnico = "ONLINE"
                if estado == 'activo':
                    color = "green"   # Todo perfecto
                    diag = "Conexión estable"
                else:
                    color = "orange"  # Raro: Suspendido pero navegando
                    diag = "ALERTA: Suspendido con servicio activo"
            else:
                estado_tecnico = "OFFLINE"
                if estado == 'activo':
                    color = "rose"    # Falla técnica
                    diag = "Sin conexión al Router (Posible falla)"
                else:
                    color = "gray"    # Normal: Está cortado
                    diag = "Corte administrativo"

            detalle.append({
                "ip": ip,
                "estado_tecnico": estado_tecnico,
                "color": color,
                "diagnostico_sistema": diag
            })
            
        return {"detalle_clientes": detalle}

    # ==========================================
    # HELPERS PRIVADOS
    # ==========================================
    async def _escanear_red_masivo(self):
        """
        Conecta a TODOS los routers activos y obtiene lista unificada de conectados.
        """
        routers = (await self.db.execute(select(RouterModel).where(RouterModel.is_active == True))).scalars().all()
        conectados = set() # Usamos Set para búsqueda O(1)

        for r in routers:
            try:
                # Conexión rápida (timeout corto idealmente)
                mk = MikroTikService(r.ip_vpn, r.user_api, r.pass_api, r.port_api)
                
                # 1. PPPoE Active
                active = mk.obtener_todos_active_pppoe()
                for item in active:
                    if 'name' in item: conectados.add(item['name'])
                    if 'address' in item: conectados.add(item['address']) 
                
                # 2. (Opcional) ARP si usas DHCP/Simple Queue
                # arp = mk.obtener_todos_arp() ...

            except Exception as e:
                # Logueamos pero NO detenemos el dashboard. Si un router falla, mostramos lo que hay.
                print(f"⚠️ Dashboard Warn: Router {r.nombre} inalcanzable ({e})")
        
        return conectados

    async def _obtener_ultimos_pagos(self):
        """Últimos 5 pagos para el widget de actividad reciente"""
        stmt = select(PagoModel).options(
            joinedload(PagoModel.cliente), 
            joinedload(PagoModel.usuario)
        ).order_by(desc(PagoModel.id)).limit(5)
        
        res = (await self.db.execute(stmt)).scalars().all()
        lista = []
        for p in res:
            # Formato fecha amigable (Hace 5 min, Hoy 10:00, etc) o simple string
            try:
                # Intentamos convertir a zona horaria local si fecha_pago tiene tz
                fecha_mx = p.fecha_pago.astimezone(self.tz_mexico).strftime("%d/%m %H:%M")
            except:
                # Fallback si fecha_pago es naive
                fecha_mx = p.fecha_pago.strftime("%d/%m %H:%M")

            lista.append({
                "cliente": p.cliente.nombre if p.cliente else "Cliente Eliminado",
                "monto": float(p.monto_total),
                "cobrador": p.usuario.nombre_completo if p.usuario else "Sistema",
                "fecha": fecha_mx
            })
        return lista