from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from typing import Optional, List, Any
from enum import Enum
from datetime import datetime, date
from decimal import Decimal

# ==========================================
# 0. ENUMS GLOBALES
# ==========================================
class TipoSeguridadEnum(str, Enum):
    pppoe = "pppoe"
    dhcp = "dhcp"

class TipoControlEnum(str, Enum):
    colas_dinamicas = "colas_dinamicas"
    colas_estaticas = "colas_estaticas"


class TipoFacturacionEnum(str, Enum):
    prepago = "prepago"
    postpago = "postpago"


class CicloFacturacionEnum(str, Enum):
    calendario = "calendario"
    aniversario = "aniversario"

# ==========================================
# 1. CONFIGURACIÓN DEL SISTEMA
# ==========================================
class SystemConfigUpdate(BaseModel):
    activar_corte_automatico: bool
    hora_ejecucion_corte: str
    hora_generacion_facturas: str = "06:00"
    hora_recordatorios: str = "09:00"
    recordatorio_1_dias: int
    recordatorio_2_dias: int
    recordatorio_3_dias: int
    activar_notificaciones: bool
    generar_facturas_automaticamente: bool
    dia_generacion_factura: int = 1
    aviso_pantalla_corte: bool
    telefonos_alerta: Optional[str] = ""

class ConfigUpdate(BaseModel):
    valor: str

# ==========================================
# 2. PLANTILLAS DE MENSAJES
# ==========================================
class MessageTemplateRequest(BaseModel):
    tipo: str
    texto: str
    activo: bool = True

class PlantillaMensajeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tipo: str
    texto: str
    activo: bool

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
    impuesto: Decimal = Field(
        default=Decimal("0.00"),
        ge=0,
        max_digits=5,
        decimal_places=2,
    )
    recordatorio_whatsapp: bool = True
    aviso_factura: str = "whatsapp"

class PlantillaResponse(BillingTemplateRequest):
    model_config = ConfigDict(from_attributes=True)

    id: int

# ==========================================
# 4. ZONAS
# ==========================================
class ZonaBase(BaseModel):
    nombre: str

class ZonaCreate(ZonaBase):
    pass

class ZonaResponse(ZonaBase):
    model_config = ConfigDict(from_attributes=True)

    id: int

# ==========================================
# 5. ROUTERS (MOVIDO ARRIBA PARA EVITAR NameError)
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

# 👇 SCHEMA NUEVO PARA EVITAR ERROR 422 AL EDITAR 👇
class RouterUpdate(BaseModel):
    nombre: Optional[str] = None
    ip_vpn: Optional[str] = None
    user_api: Optional[str] = None
    pass_api: Optional[str] = None # Opcional para no borrarla si no se envía
    port_api: Optional[int] = None
    tipo_seguridad: Optional[TipoSeguridadEnum] = None
    tipo_control: Optional[TipoControlEnum] = None
    version_os: Optional[str] = None

class RouterResponse(RouterBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    is_active: bool
    created_at: datetime

# ==========================================
# 6. CAJAS NAP
# ==========================================
class CajaNapBase(BaseModel):
    nombre: str = Field(min_length=2, max_length=100)
    ubicacion: str = Field(min_length=2, max_length=200)
    coordenadas: Optional[str] = None
    capacidad: int = Field(default=16, ge=1, le=128)
    zona_id: int
    olt_id: Optional[int] = None
    puerto_olt: Optional[int] = None

class CajaNapCreate(CajaNapBase):
    pass

class CajaNapResponse(CajaNapBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    zona_nombre: Optional[str] = None
    olt_nombre: Optional[str] = None
    router_id: Optional[int] = None
    router_nombre: Optional[str] = None
    puertos_usados: int = 0
    puertos_libres: int = 0


# ==========================================
# 6.5. OLTs (Infraestructura de Fibra)
# ==========================================
class OLTBase(BaseModel):
    nombre: str = Field(..., min_length=3, max_length=100)
    ip: str = Field(..., min_length=7, max_length=15)
    comunidad: str = "public"
    tecnologia: str = Field(..., pattern="^(GPON|EPON)$") # Asegura que solo envíen "GPON" o "EPON"
    modelo: Optional[str] = None
    router_id: Optional[int] = None

    # VSOL API JSON / Web API
    tipo_integracion: Optional[str] = "snmp"  # snmp | vsol_api | auto
    api_enabled: Optional[bool] = False
    api_protocol: Optional[str] = "https"
    api_port: Optional[int] = 443
    api_user: Optional[str] = None
    api_verify_ssl: Optional[bool] = False

class OLTCreate(OLTBase):
    api_password: Optional[str] = None

class OLTUpdate(BaseModel):
    nombre: Optional[str] = None
    ip: Optional[str] = None
    comunidad: Optional[str] = None
    tecnologia: Optional[str] = Field(None, pattern="^(GPON|EPON)$")
    modelo: Optional[str] = None
    router_id: Optional[int] = None
    is_active: Optional[bool] = None

    # VSOL API JSON / Web API
    tipo_integracion: Optional[str] = None
    api_enabled: Optional[bool] = None
    api_protocol: Optional[str] = None
    api_port: Optional[int] = None
    api_user: Optional[str] = None
    api_password: Optional[str] = None
    api_verify_ssl: Optional[bool] = None

class OLTResponse(OLTBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    is_active: bool
    created_at: datetime

class ONUSimple(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    identificador: str
    modelo: Optional[str] = None
    tecnologia: str
    estado: str



class ONURetorno(BaseModel):
    id: int
    identificador: str
    tecnologia: str
    modelo: str
    estado: str
    tecnico_id: Optional[int] = None
    cliente_id: Optional[int] = None
    cliente_nombre: Optional[str] = None
    cliente_direccion: Optional[str] = None
    cliente_zona: Optional[str] = None  #

# ==========================================
# 7. PLANES
# ==========================================
class PlanBase(BaseModel):
    nombre: str = Field(..., min_length=3)
    precio: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    router_id: int
    garantia_percent: int = Field(default=100, ge=1, le=100) 
    prioridad: int = Field(default=8, ge=1, le=8)            
    burst_subida: int = Field(default=0, ge=0)
    burst_bajada: int = Field(default=0, ge=0)
    burst_time: int = Field(default=0, ge=0)

class PlanCreate(PlanBase):
    subida_kbps: int
    bajada_kbps: int

class PlanResponse(PlanBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    velocidad_subida: int 
    velocidad_bajada: int

# ==========================================
# 8. REDES
# ==========================================
class RedBase(BaseModel):
    nombre: str
    cidr: str
    gateway: Optional[str] = None
    router_id: int

class RedCreate(RedBase):
    pass

class RedResponse(RedBase):
    model_config = ConfigDict(from_attributes=True)

    id: int

# ==========================================
# 9. USUARIOS (STAFF)
# ==========================================
class UsuarioBase(BaseModel):
    nombre_completo: str = Field(..., min_length=3, max_length=100)
    usuario: str = Field(..., min_length=3, max_length=50, pattern=r"^[A-Za-z0-9_.-]+$")
    rol: str = Field(default="cajero", pattern=r"^(admin|cajero|tecnico|supervisor)$")
    activo: bool = True

class UsuarioCreate(UsuarioBase):
    password: str = Field(..., min_length=10, max_length=128)
    router_ids: List[int] = Field(default_factory=list)

class UsuarioUpdate(BaseModel):
    nombre_completo: Optional[str] = None
    usuario: Optional[str] = None
    password: Optional[str] = None
    rol: Optional[str] = None
    activo: Optional[bool] = None
    router_ids: Optional[List[int]] = None

class UsuarioResponse(UsuarioBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    router_ids: List[int] = Field(default_factory=list)

# ==========================================
# 10. CLIENTES
# ==========================================
class ClienteBase(BaseModel):
    nombre: str
    cedula: Optional[str] = None 
    onu_id: int | None = None
    identificador_onu: Optional[str] = None
    telefono: Optional[str] = None
    direccion: Optional[str] = None
    correo: Optional[str] = None
    latitud: Optional[float] = None
    longitud: Optional[float] = None
    router_id: Optional[int] = None
    plan_id: Optional[int] = None
    tecnico_id: Optional[int] = None
    plantilla_id: Optional[int] = None
    zona_id: Optional[int] = None
    red_id: Optional[int] = None
    olt_id: Optional[int] = None
    caja_nap_id: Optional[int] = None
    puerto_nap: Optional[int] = None
    ip_asignada: Optional[str] = "0.0.0.0"
    user_pppoe: Optional[str] = None
    pass_pppoe: Optional[str] = None
    mac_address: Optional[str] = None
    estado: str = "pendiente_instalacion"

class ClienteCreate(ClienteBase):
    pass

class ClienteResponse(ClienteBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    estado: str
    created_at: datetime
    saldo_a_favor: Decimal = Decimal("0.00")
    router: Optional[RouterResponse] = None
    zona: Optional[ZonaResponse] = None          
    plantilla: Optional[PlantillaResponse] = None 
    plan: Optional[PlanResponse] = None
    caja_nap: Optional[CajaNapResponse] = None
    tecnico: Optional[UsuarioResponse] = None
    olt: Optional[OLTResponse] = None
    onu_asignada: Optional[ONUSimple] = None





# EL TÉCNICO EN CAMPO
class InstalacionRequest(BaseModel):
    cedula: Optional[str] = None
    onu_id: Optional[int] = None
    mac_address: Optional[str] = None
    olt_id: Optional[int] = None
    caja_nap_id: Optional[int] = None
    puerto_nap: Optional[int] = None
    latitud: Optional[float] = None
    longitud: Optional[float] = None
    plan_id: Optional[int] = None
    router_id: Optional[int] = None

    user_pppoe: str
    pass_pppoe: str
    ip_asignada: Optional[str] = None

    fecha_instalacion: Optional[date] = None
    fecha_activacion: Optional[date] = None
    tipo_facturacion: TipoFacturacionEnum = TipoFacturacionEnum.prepago
    ciclo_facturacion: CicloFacturacionEnum = CicloFacturacionEnum.calendario
    meses_gratis: int = Field(default=1, ge=0, le=12)
    potencia_optica_dbm: Optional[float] = Field(default=None, ge=-50, le=10)
    potencia_tx_dbm: Optional[float] = Field(default=None, ge=-50, le=20)
    observaciones_opticas: Optional[str] = Field(default=None, max_length=500)


# ==========================================
# 10.5 SERVICIOS / CONTRATOS POR DOMICILIO
# ==========================================
class ServicioCreate(BaseModel):
    cliente_id: int
    alias: str = Field(default="Principal", min_length=2, max_length=100)
    direccion: str = Field(min_length=5, max_length=255)
    latitud: Optional[float] = Field(default=None, ge=-90, le=90)
    longitud: Optional[float] = Field(default=None, ge=-180, le=180)
    router_id: Optional[int] = None
    plan_id: Optional[int] = None
    plantilla_id: Optional[int] = None
    zona_id: Optional[int] = None
    red_id: Optional[int] = None
    tecnico_id: Optional[int] = None
    tipo_facturacion: TipoFacturacionEnum = TipoFacturacionEnum.prepago
    ciclo_facturacion: CicloFacturacionEnum = CicloFacturacionEnum.calendario
    meses_gratis: int = Field(default=0, ge=0, le=12)
    crear_orden: bool = True


class ServicioUpdate(BaseModel):
    alias: Optional[str] = Field(default=None, min_length=2, max_length=100)
    direccion: Optional[str] = Field(default=None, min_length=5, max_length=255)
    latitud: Optional[float] = Field(default=None, ge=-90, le=90)
    longitud: Optional[float] = Field(default=None, ge=-180, le=180)
    router_id: Optional[int] = None
    plan_id: Optional[int] = None
    plantilla_id: Optional[int] = None
    zona_id: Optional[int] = None
    red_id: Optional[int] = None
    tecnico_id: Optional[int] = None
    tipo_facturacion: Optional[TipoFacturacionEnum] = None
    ciclo_facturacion: Optional[CicloFacturacionEnum] = None
    meses_gratis: Optional[int] = Field(default=None, ge=0, le=12)


class ServicioActivacion(BaseModel):
    router_id: Optional[int] = None
    plan_id: Optional[int] = None
    plantilla_id: Optional[int] = None
    zona_id: Optional[int] = None
    red_id: Optional[int] = None
    olt_id: Optional[int] = None
    caja_nap_id: Optional[int] = None
    puerto_nap: Optional[int] = None
    onu_id: Optional[int] = None
    tecnico_id: Optional[int] = None
    ip_asignada: Optional[str] = None
    mac_address: Optional[str] = None
    user_pppoe: str = Field(min_length=1, max_length=50)
    pass_pppoe: str = Field(min_length=3, max_length=100)
    fecha_instalacion: Optional[date] = None
    fecha_activacion: Optional[date] = None
    tipo_facturacion: TipoFacturacionEnum = TipoFacturacionEnum.prepago
    ciclo_facturacion: CicloFacturacionEnum = CicloFacturacionEnum.calendario
    meses_gratis: int = Field(default=0, ge=0, le=12)


class ServicioEstadoUpdate(BaseModel):
    estado: str = Field(pattern="^(activo|suspendido)$")


class ServicioPlanUpdate(BaseModel):
    plan_id: int = Field(gt=0)


class ServicioPlanCatalogo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    nombre: str
    precio: Decimal
    router_id: Optional[int] = None


class ServicioRouterCatalogo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    nombre: str


class ServicioResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    cliente_id: int
    alias: str
    direccion: Optional[str] = None
    latitud: Optional[float] = None
    longitud: Optional[float] = None
    router_id: Optional[int] = None
    plan_id: Optional[int] = None
    plantilla_id: Optional[int] = None
    zona_id: Optional[int] = None
    red_id: Optional[int] = None
    olt_id: Optional[int] = None
    caja_nap_id: Optional[int] = None
    puerto_nap: Optional[int] = None
    tecnico_id: Optional[int] = None
    onu_id: Optional[int] = None
    ip_asignada: Optional[str] = None
    mac_address: Optional[str] = None
    user_pppoe: Optional[str] = None
    is_online: bool = False
    estado: str
    tipo_facturacion: TipoFacturacionEnum
    ciclo_facturacion: CicloFacturacionEnum
    fecha_instalacion: Optional[date] = None
    fecha_activacion: Optional[date] = None
    fecha_inicio_cobro: Optional[date] = None
    proxima_facturacion: Optional[date] = None
    meses_gratis: int
    created_at: datetime
    plan: Optional[ServicioPlanCatalogo] = None
    router: Optional[ServicioRouterCatalogo] = None


class ServicioPlanUpdateResponse(BaseModel):
    servicio: ServicioResponse
    mikrotik_sincronizado: Optional[bool] = None
    mensaje: str

# ==========================================
# 11. FACTURAS
# ==========================================
class ClienteSimple(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    nombre: str
    telefono: Optional[str] = None
    saldo_a_favor: Decimal = Decimal("0.00")

class FacturaBase(BaseModel):
    fecha_emision: date
    fecha_vencimiento: date
    plan_snapshot: Optional[str] = None
    detalles: Optional[str] = None
    monto: Decimal
    impuesto: Decimal = Decimal("0.00")
    total: Decimal
    saldo_pendiente: Decimal
    estado: str
    mes_correspondiente: str
    fecha_promesa_pago: Optional[date] = None
    es_promesa_activa: bool = False

    servicio_id: Optional[int] = None
    periodo_desde: Optional[date] = None
    periodo_hasta: Optional[date] = None
    dias_facturados: Optional[int] = None
    dias_periodo: Optional[int] = None
    precio_mensual_snapshot: Optional[Decimal] = None
    precio_diario: Optional[Decimal] = None
    es_prorrateada: bool = False
    tipo_facturacion_snapshot: Optional[str] = None
    ciclo_facturacion_snapshot: Optional[str] = None

class FacturaCreate(FacturaBase):
    cliente_id: int

class FacturaResponse(FacturaBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    cliente: Optional[ClienteSimple] = None 

# ==========================================
# 12. UNIFICADO (DASHBOARD/MAPA)
# ==========================================
class FacturacionResumen(BaseModel):
    facturas_pendientes_cant: int
    total_deuda: Decimal
    saldo_a_favor: Decimal
    estado_financiero: str

class ServicioTecnico(BaseModel):
    plan_nombre: str
    precio_plan: Decimal
    ip_asignada: str
    router_nombre: str
    estado_servicio: str
    nap_info: Optional[str] = None

class ClienteFullResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    nombre: str
    cedula: Optional[str] = None
    telefono: Optional[str] = None
    direccion: Optional[str] = None
    latitud: Optional[float] = None
    longitud: Optional[float] = None
    caja_nap_id: Optional[int] = None
    puerto_nap: Optional[int] = None
    zona: Optional[str] = "Sin Zona"
    servicio: ServicioTecnico
    finanzas: FacturacionResumen

class LogCronjobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    fecha: datetime
    nivel: str
    origen: str
    mensaje: str


# Conexion VPN


class WireguardConfigResponse(BaseModel):
    clientIp: str
    clientPrivKey: str
    serverPubKey: str
    serverEndpoint: str
    serverPort: int


class VpnTunnelCreate(BaseModel):
    nombre: str

class VpnTunnelResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    nombre: str
    ip_asignada: str
    public_key: str
    script_mikrotik: str
    is_active: bool
    created_at: datetime
