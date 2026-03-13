from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Optional, List, Any
from enum import Enum
from datetime import datetime, date

# ==========================================
# 0. ENUMS GLOBALES
# ==========================================
class TipoSeguridadEnum(str, Enum):
    pppoe = "pppoe"
    dhcp = "dhcp"

class TipoControlEnum(str, Enum):
    colas_dinamicas = "colas_dinamicas"
    colas_estaticas = "colas_estaticas"

# ==========================================
# 1. CONFIGURACIÓN DEL SISTEMA (PANEL DE CONTROL)
# ==========================================
class SystemConfigUpdate(BaseModel):
    activar_corte_automatico: bool
    hora_ejecucion_corte: str
    recordatorio_1_dias: int
    recordatorio_2_dias: int
    recordatorio_3_dias: int
    activar_notificaciones: bool
    generar_facturas_automaticamente: bool
    dia_generacion_factura: int = 1
    aviso_pantalla_corte: bool

class ConfigUpdate(BaseModel):
    valor: str

# ==========================================
# 2. PLANTILLAS DE MENSAJES (WHATSAPP)
# ==========================================
class MessageTemplateRequest(BaseModel):
    tipo: str
    texto: str
    activo: bool = True

class PlantillaMensajeResponse(BaseModel):
    id: int
    tipo: str
    texto: str
    activo: bool
    class Config:
        from_attributes = True

class PlantillaMensajeUpdate(BaseModel):
    texto: str
    activo: bool

# ==========================================
# 3. PLANTILLAS DE FACTURACIÓN
# ==========================================
class BillingTemplateRequest(BaseModel):
    nombre: str
    dias_antes_emision: int
    dia_pago: int
    dias_tolerancia: int
    impuesto: float = 0
    recordatorio_whatsapp: bool = True
    aviso_factura: str = "whatsapp"

class PlantillaResponse(BillingTemplateRequest):
    id: int
    class Config:
        from_attributes = True

# ==========================================
# 4. ZONAS
# ==========================================
class ZonaBase(BaseModel):
    nombre: str

class ZonaCreate(ZonaBase):
    pass

class ZonaResponse(ZonaBase):
    id: int
    class Config:
        from_attributes = True

# ==========================================
# 4.1 CAJAS NAP (INFRAESTRUCTURA FIBRA) ✅ NUEVO
# ==========================================
class CajaNapBase(BaseModel):
    nombre: str
    ubicacion: str
    coordenadas: Optional[str] = None
    capacidad: int = 16
    zona_id: int

class CajaNapCreate(CajaNapBase):
    pass

class CajaNapResponse(CajaNapBase):
    id: int
    # Campos calculados (No están en BD, se llenan en el endpoint)
    puertos_usados: int = 0
    puertos_libres: int = 0
    
    class Config:
        from_attributes = True

# ==========================================
# 5. ROUTERS
# ==========================================
class RouterBase(BaseModel):
    nombre: str = Field(..., min_length=3, max_length=100)
    ip_vpn: str
    user_api: str = "admin"
    port_api: int = 8728
    tipo_seguridad: TipoSeguridadEnum = TipoSeguridadEnum.pppoe
    tipo_control: TipoControlEnum = TipoControlEnum.colas_dinamicas
    version_os: str = "v7"

class RouterCreate(RouterBase):
    pass_api: str = Field(..., min_length=3)

class RouterResponse(RouterBase):
    id: int
    is_active: bool
    created_at: datetime
    class Config:
        from_attributes = True

# ==========================================
# 6. PLANES
# ==========================================
class PlanBase(BaseModel):
    nombre: str = Field(..., min_length=3)
    precio: float
    router_id: int
    
    # QoS / Ráfagas
    garantia_percent: int = Field(default=100, ge=1, le=100) 
    prioridad: int = Field(default=8, ge=1, le=8)            
    burst_subida: int = Field(default=0, ge=0)
    burst_bajada: int = Field(default=0, ge=0)
    burst_time: int = Field(default=0, ge=0)

class PlanCreate(PlanBase):
    subida_kbps: int
    bajada_kbps: int

class PlanResponse(PlanBase):
    id: int
    velocidad_subida: int 
    velocidad_bajada: int

    class Config:
        from_attributes = True

    @model_validator(mode='before')
    @classmethod
    def map_velocidades_db_to_schema(cls, data: Any) -> Any:
        if hasattr(data, 'velocidad_subida'):
            return data
        return data

# ==========================================
# 7. REDES
# ==========================================
class RedBase(BaseModel):
    nombre: str
    cidr: str
    gateway: Optional[str] = None
    router_id: int

class RedCreate(RedBase):
    pass

class RedResponse(RedBase):
    id: int
    class Config:
        from_attributes = True

# ==========================================
# 8. USUARIOS (STAFF)
# ==========================================
class UsuarioBase(BaseModel):
    nombre_completo: str
    usuario: str
    rol: str = "cajero"
    activo: bool = True

class UsuarioCreate(UsuarioBase):
    password: str
    router_ids: List[int] = []

class UsuarioUpdate(BaseModel):
    nombre_completo: Optional[str] = None
    usuario: Optional[str] = None
    password: Optional[str] = None
    rol: Optional[str] = None
    activo: Optional[bool] = None
    router_ids: Optional[List[int]] = None

class UsuarioResponse(UsuarioBase):
    id: int
    router_ids: List[int] = [] 

    class Config:
        from_attributes = True

    @model_validator(mode='before')
    @classmethod
    def map_routers_to_ids(cls, data: Any) -> Any:
        if hasattr(data, 'routers_asignados'):
            data.router_ids = [r.id for r in data.routers_asignados]
        return data

# ==========================================
# 9. CLIENTES
# ==========================================
class ClienteBase(BaseModel):
    nombre: str
    cedula: Optional[str] = None 
    telefono: Optional[str] = None
    direccion: Optional[str] = None
    correo: Optional[str] = None
    
    # 👇 ESTOS ERAN EL PROBLEMA (Antes decían "int" a secas)
    router_id: Optional[int] = None   # Acepta null
    plan_id: Optional[int] = None     # Acepta null
    
    # 👇 ESTE FALTABA AGREGAR (El front lo manda, el back no lo esperaba)
    tecnico_id: Optional[int] = None
    
    plantilla_id: Optional[int] = None
    zona_id: Optional[int] = None
    red_id: Optional[int] = None
    
    # Campos FTTH
    caja_nap_id: Optional[int] = None
    puerto_nap: Optional[int] = None
    
    # Técnicos
    ip_asignada: Optional[str] = "0.0.0.0"
    user_pppoe: Optional[str] = None
    pass_pppoe: Optional[str] = None
    mac_address: Optional[str] = None
    
    # Estado y Servicio
    estado: str = "pendiente_instalacion"

class ClienteCreate(ClienteBase):
    pass

class ClienteResponse(ClienteBase):
    id: int
    estado: str
    created_at: datetime
    saldo_a_favor: float = 0.0
    
    # Relaciones anidadas
    router: Optional[RouterResponse] = None
    zona: Optional[ZonaResponse] = None          
    plantilla: Optional[PlantillaResponse] = None 
    plan: Optional[PlanResponse] = None
    
    # 👇 Relación anidada para mostrar el nombre de la caja en frontend
    caja_nap: Optional[CajaNapResponse] = None
    
    class Config:
        from_attributes = True

# ==========================================
# 10. FACTURAS
# ==========================================
class ClienteSimple(BaseModel):
    id: int
    nombre: str
    telefono: Optional[str] = None
    saldo_a_favor: float = 0.0
    class Config:
        from_attributes = True

class FacturaBase(BaseModel):
    fecha_emision: date
    fecha_vencimiento: date
    
    plan_snapshot: Optional[str] = None
    detalles: Optional[str] = None
    
    monto: float
    impuesto: float = 0.0
    total: float
    saldo_pendiente: float
    
    estado: str
    mes_correspondiente: str
    
    fecha_promesa_pago: Optional[date] = None
    es_promesa_activa: bool = False

class FacturaCreate(FacturaBase):
    cliente_id: int

class FacturaResponse(FacturaBase):
    id: int
    cliente: Optional[ClienteSimple] = None 

    class Config:
        from_attributes = True

# ==========================================
# 11. UNIFICADO (DASHBOARD/RESUMEN)
# ==========================================

# Bloque Financiero
class FacturacionResumen(BaseModel):
    facturas_pendientes_cant: int
    total_deuda: float
    saldo_a_favor: float
    estado_financiero: str  # "al_dia" o "moroso"

# Bloque Técnico
class ServicioTecnico(BaseModel):
    plan_nombre: str
    precio_plan: float
    ip_asignada: str
    router_nombre: str
    estado_servicio: str # activo/suspendido

# El SUPER SCHEMA para vista detallada de cliente
class ClienteFullResponse(BaseModel):
    id: int
    nombre: str
    cedula: Optional[str] = None
    telefono: Optional[str] = None
    direccion: Optional[str] = None
    
    # Agregar aquí también si necesitas ver la NAP en el detalle completo
    caja_nap_id: Optional[int] = None
    puerto_nap: Optional[int] = None
    
    servicio: ServicioTecnico
    finanzas: FacturacionResumen

    class Config:
        from_attributes = True

# ==========================================
# 12. LOGS DE CRONJOBS (HISTORIAL)
# ==========================================
class LogCronjobResponse(BaseModel):
    id: int
    fecha: datetime
    nivel: str      # 'INFO', 'ERROR', 'WARNING'
    origen: str     # 'Facturación', 'Cortes', 'Sistema'
    mensaje: str

    class Config:
        from_attributes = True