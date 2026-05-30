import enum
from sqlalchemy import Column, Date, Integer, String, Boolean, DateTime, Enum, ForeignKey, Float, Text, Table
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from .database import Base
from datetime import datetime
from sqlalchemy.dialects.mysql import LONGTEXT

# ==========================================
# 1. ENUMS Y TABLAS INTERMEDIAS
# ==========================================
class TipoSeguridad(str, enum.Enum):
    pppoe = "pppoe"
    dhcp = "dhcp"

class TipoControl(str, enum.Enum):
    colas_dinamicas = "colas_dinamicas"
    colas_estaticas = "colas_estaticas"

usuario_routers_association = Table(
    'usuario_routers',
    Base.metadata,
    Column('usuario_id', Integer, ForeignKey('usuarios.id'), primary_key=True),
    Column('router_id', Integer, ForeignKey('routers.id'), primary_key=True)
)

class EstadoEquipo(str, enum.Enum):
    INSTALADO = "instalado"
    POR_RECOGER = "por_recoger"
    EN_BODEGA = "en_bodega"


class InventarioONUModel(Base):
    __tablename__ = "inventario_onus"

    id = Column(Integer, primary_key=True, index=True)
    # MAC para EPON, SN para GPON
    identificador = Column(String(100), unique=True, index=True, nullable=False)
    
    tecnologia = Column(String(20), nullable=False) # 'GPON' o 'EPON'
    modelo = Column(String(100), nullable=True)     # Ej: 'ZTE F670L'
    
    # Estados: DISPONIBLE, INSTALADO, POR_RECOGER, DAÑADO
    estado = Column(String(50), default='DISPONIBLE', nullable=False)
    
    # Quién tiene el equipo si no está en bodega
    tecnico_id = Column(Integer, ForeignKey('usuarios.id'), nullable=True) 
    
    created_at = Column(DateTime, default=func.now())

    # Relaciones
    cliente = relationship("ClienteModel", back_populates="onu_asignada", uselist=False)
    tecnico = relationship("UsuarioModel")

# ==========================================
# 2. INFRAESTRUCTURA (Routers, Redes, Planes, NAPs)
# ==========================================
class RouterModel(Base):
    __tablename__ = "routers"
    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String(100), nullable=False)
    ip_vpn = Column(String(50), nullable=False, unique=True)
    user_api = Column(String(50), default="admin")
    pass_api = Column(String(100), nullable=False)
    port_api = Column(Integer, default=8728)
    tipo_seguridad = Column(Enum(TipoSeguridad), default=TipoSeguridad.pppoe)
    tipo_control = Column(Enum(TipoControl), default=TipoControl.colas_dinamicas)
    version_os = Column(String(10), default="v7")
    is_active = Column(Boolean, default=True)
    is_online = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    planes = relationship("PlanModel", back_populates="router")
    clientes = relationship("ClienteModel", back_populates="router")
    redes = relationship("RedModel", back_populates="router")

class RedModel(Base):
    __tablename__ = "redes"
    id = Column(Integer, primary_key=True, index=True)
    router_id = Column(Integer, ForeignKey("routers.id"))
    nombre = Column(String(100))
    cidr = Column(String(50)) 
    gateway = Column(String(50))
    router = relationship("RouterModel", back_populates="redes")
    clientes = relationship("ClienteModel", back_populates="red")

class PlanModel(Base):
    __tablename__ = "planes"
    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String(100)) 
    precio = Column(Float)
    
    # Velocidad Normal (Max-Limit)
    velocidad_subida = Column(Integer)
    velocidad_bajada = Column(Integer)
    
    # QoS Básico
    garantia_percent = Column(Integer, default=100) 
    prioridad = Column(Integer, default=8)          
    
    # Ráfagas (Burst)
    burst_subida = Column(Integer, default=0)
    burst_bajada = Column(Integer, default=0)
    burst_time = Column(Integer, default=0)         
    
    router_id = Column(Integer, ForeignKey("routers.id"))
    router = relationship("RouterModel", back_populates="planes")
    clientes = relationship("ClienteModel", back_populates="plan")

class ZonaModel(Base):
    __tablename__ = "zonas"
    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String(100))
    
    clientes = relationship("ClienteModel", back_populates="zona")
    # Relación inversa con NAPs
    cajas_nap = relationship("CajaNapModel", back_populates="zona")

# 👇👇👇 NUEVA TABLA PARA CAJAS NAP 👇👇👇
class CajaNapModel(Base):
    __tablename__ = "cajas_nap"
    
    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String(100))        # Ej: "NAP-01-Centro"
    ubicacion = Column(String(200))     # Ej: "Poste 54, Calle Hidalgo"
    coordenadas = Column(String(100))   # Opcional para mapa
    capacidad = Column(Integer, default=16) # 8 o 16 puertos
    puerto_olt = Column(Integer, nullable=True)
    
    # Relación con Zona (Para filtrar por colonia)
    zona_id = Column(Integer, ForeignKey("zonas.id"))
    zona = relationship("ZonaModel", back_populates="cajas_nap")
    
    # Relación con Clientes
    olt_id = Column(Integer, ForeignKey("olts.id"), nullable=True)
    puerto_olt = Column(Integer, nullable=True)
    olt = relationship("OLTModel", back_populates="cajas_nap")
    clientes = relationship("ClienteModel", back_populates="caja_nap")


class OLTModel(Base):
    __tablename__ = "olts"
    
    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String(100), nullable=False)        # Ej: "OLT San Lucas"
    ip = Column(String(50), nullable=False, unique=True)
    comunidad = Column(String(50), default="public")
    tecnologia = Column(String(20), nullable=False)     # "GPON" o "EPON"
    modelo = Column(String(50))                         # "V1600GS", "V1601E02-DP", "HIOSO"
    
    router_id = Column(Integer, ForeignKey("routers.id"), nullable=True) # A qué MikroTik está conectada
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relaciones
    router = relationship("RouterModel", backref="olts")
    clientes = relationship("ClienteModel", back_populates="olt")
    cajas_nap = relationship("CajaNapModel", back_populates="olt") # Opcional: Para mapear la red física





# ==========================================
# 3. CLIENTES Y FACTURACIÓN
# ==========================================
class PlantillaFacturacionModel(Base):
    __tablename__ = "plantillas_facturacion" 
    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String(100))
    # Datos para la lógica individual
    dia_pago = Column(Integer, default=1)           
    dias_antes_emision = Column(Integer, default=5) 
    dias_tolerancia = Column(Integer, default=3)    
    impuesto = Column(Float, default=0.0) 
    recordatorio_whatsapp = Column(Boolean, default=True)
    aviso_factura = Column(String(50), default='whatsapp') 
    
    clientes = relationship("ClienteModel", back_populates="plantilla")

class ClienteModel(Base):
    __tablename__ = "clientes"
    
    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String(150), nullable=False)
    cedula = Column(String(50), unique=True, index=True) 
    
    telefono = Column(String(20))
    direccion = Column(String(200))
    correo = Column(String(100))
    
    # ... (Tus campos de IP, MAC y PPPoE se mantienen igual)
    ip_asignada = Column(String(20), unique=True)
    mac_address = Column(String(20))
    user_pppoe = Column(String(50))
    pass_pppoe = Column(String(50))
    
    # ... (Tus llaves foráneas se mantienen igual)
    router_id = Column(Integer, ForeignKey("routers.id"))
    plan_id = Column(Integer, ForeignKey("planes.id"))
    zona_id = Column(Integer, ForeignKey("zonas.id"))
    red_id = Column(Integer, ForeignKey("redes.id"))
    plantilla_id = Column(Integer, ForeignKey("plantillas_facturacion.id"))
    olt_id = Column(Integer, ForeignKey("olts.id"), nullable=True)

    # 👇👇👇 CAMPOS FTTH 👇👇👇
    caja_nap_id = Column(Integer, ForeignKey("cajas_nap.id"), nullable=True)
    puerto_nap = Column(Integer, nullable=True)
    tecnico_id = Column(Integer, ForeignKey("usuarios.id"), nullable=True)
    onu_id = Column(Integer, ForeignKey('inventario_onus.id'), nullable=True)
    onu_asignada = relationship("InventarioONUModel", back_populates="cliente")
    
    # ... (Tus relaciones se mantienen igual)
    tecnico = relationship("UsuarioModel", foreign_keys=[tecnico_id])
    router = relationship("RouterModel", back_populates="clientes")
    plan = relationship("PlanModel", back_populates="clientes")
    zona = relationship("ZonaModel", back_populates="clientes")
    red = relationship("RedModel", back_populates="clientes")
    plantilla = relationship("PlantillaFacturacionModel", back_populates="clientes")
    caja_nap = relationship("CajaNapModel", back_populates="clientes")
    olt = relationship("OLTModel", back_populates="clientes")
    
    
    estado = Column(String(50), default="activo")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    proxima_factura = Column(Date) 
    saldo_a_favor = Column(Float, default=0.0)

    facturas = relationship("FacturaModel", back_populates="cliente")
    pagos = relationship("PagoModel", back_populates="cliente")

    latitud = Column(Float, nullable=True)
    longitud = Column(Float, nullable=True)
    is_online = Column(Boolean, default=False)
    ultimo_cambio_estado = Column(DateTime, default=func.now(), onupdate=func.now())


class FacturaModel(Base):
    __tablename__ = "facturas"
    id = Column(Integer, primary_key=True, index=True)
    cliente_id = Column(Integer, ForeignKey("clientes.id"))
    
    plan_snapshot = Column(String(150))
    detalles = Column(Text)
    monto = Column(Float)
    impuesto = Column(Float, default=0)
    total = Column(Float)
    saldo_pendiente = Column(Float, default=0)
    
    fecha_emision = Column(Date)       
    fecha_vencimiento = Column(Date)
    fecha_limite_corte = Column(Date)
    fecha_pago_real = Column(DateTime, nullable=True)
    mes_correspondiente = Column(String(20)) 
    estado = Column(String(20), default="pendiente")
    
    fecha_promesa_pago = Column(Date, nullable=True)
    es_promesa_activa = Column(Boolean, default=False)
    
    cliente = relationship("ClienteModel", back_populates="facturas")
    pagos = relationship("PagoModel", back_populates="factura")

class PagoModel(Base):
    __tablename__ = "pagos"
    id = Column(Integer, primary_key=True, index=True)
    cliente_id = Column(Integer, ForeignKey("clientes.id"))
    usuario_id = Column(Integer, ForeignKey("usuarios.id"))
    factura_id = Column(Integer, ForeignKey("facturas.id"), nullable=True)
    
    monto_total = Column(Float)
    metodo_pago = Column(String(50), default="efectivo")
    referencia = Column(String(100))
    fecha_pago = Column(DateTime, default=datetime.now)
    created_at = Column(DateTime, default=datetime.now) 
    
    cliente = relationship("ClienteModel", back_populates="pagos")
    factura = relationship("FacturaModel", back_populates="pagos")
    usuario = relationship("UsuarioModel", back_populates="pagos")

# ==========================================
# 4. SISTEMA Y CONFIGURACIÓN
# ==========================================
class UsuarioModel(Base):
    __tablename__ = "usuarios"
    id = Column(Integer, primary_key=True, index=True)
    nombre_completo = Column(String(100))
    usuario = Column(String(50), unique=True)
    password_hash = Column(Text)
    rol = Column(String(20), default="tecnico") 
    activo = Column(Boolean, default=True)
    
    pagos = relationship("PagoModel", back_populates="usuario")
    
    routers_asignados = relationship(
        "RouterModel",
        secondary=usuario_routers_association,
        backref="usuarios_permitidos",
        lazy="selectin" 
    )

    @property
    def router_ids(self):
        """
        Esta propiedad es leída automáticamente por Pydantic (gracias a from_attributes=True)
        para llenar la lista de 'router_ids' en el JSON de respuesta.
        """
        return [router.id for router in self.routers_asignados] if self.routers_asignados else []

class ConfiguracionModel(Base):
    __tablename__ = "configuracion"
    id = Column(Integer, primary_key=True, index=True)
    clave = Column(String(50), unique=True) 
    valor = Column(String(100))             

class ConfiguracionSistema(Base):
    __tablename__ = "configuracion_sistema"
    id = Column(Integer, primary_key=True, index=True)
    
    # --- CORTES ---
    activar_corte_automatico = Column(Boolean, default=True)
    hora_ejecucion_corte = Column(String(10), default="03:00")
    
    # --- NOTIFICACIONES / RECORDATORIOS ---
    activar_notificaciones = Column(Boolean, default=True)
    hora_recordatorios = Column(String(10), default="09:00") # 👈 NUEVA
    recordatorio_1_dias = Column(Integer, default=5)
    recordatorio_2_dias = Column(Integer, default=1)
    recordatorio_3_dias = Column(Integer, default=0)
    
    # --- FACTURACIÓN ---
    generar_facturas_automaticamente = Column(Boolean, default=True)
    hora_generacion_facturas = Column(String(10), default="06:00") # 👈 NUEVA
    dia_generacion_factura = Column(Integer, default=1)
    
    # --- EXTRA ---
    aviso_pantalla_corte = Column(Boolean, default=False)
    telefonos_alerta = Column(String(255), default="")

class PlantillaMensajeModel(Base):
    __tablename__ = "plantillas_mensajes"
    id = Column(Integer, primary_key=True, index=True)
    tipo = Column(String(50), unique=True) 
    texto = Column(Text)    
    activo = Column(Boolean, default=True)

# ==========================================
# 5. LOGS DE CRONJOBS
# ==========================================
class LogCronjobModel(Base):
    __tablename__ = "logs_cronjobs"

    id = Column(Integer, primary_key=True, index=True)
    fecha = Column(DateTime, default=func.now()) 
    nivel = Column(String(20))
    origen = Column(String(50))
    mensaje = Column(Text)





class MensajeChatModel(Base):
    __tablename__ = "mensajes_chat"

    id = Column(Integer, primary_key=True, index=True)
    cliente_id = Column(Integer, ForeignKey("clientes.id"), nullable=True) 
    telefono = Column(String(20), index=True) 
    direccion = Column(String(10)) 
    mensaje = Column(LONGTEXT)
    tipo_mensaje = Column(String(20), default="texto") 
    leido = Column(Boolean, default=False) 
    fecha = Column(DateTime, default=func.now())
    
    # 👇 NUEVAS COLUMNAS PARA RASTREAR ESTADO DE WHATSAPP 👇
    wa_id = Column(String(100), nullable=True, index=True) # ID interno del mensaje de WhatsApp
    ack = Column(Integer, default=0) # 0=Pendiente, 1=Enviado, 2=Entregado, 3=Visto

    cliente = relationship("ClienteModel", backref="historial_chat")



class VpnTunnelModel(Base):
    __tablename__ = "vpn_tunnels"

    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String(100), nullable=False) # Ej: "Torre Principal", "Cliente Juan"
    ip_asignada = Column(String(20), unique=True, nullable=False)
    public_key = Column(String(100), nullable=False)
    script_mikrotik = Column(Text, nullable=True) # Guardamos el script por si quieres volver a verlo
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=func.now())


class PagoAutovalidadoModel(Base):
    __tablename__ = "pagos_autovalidados"

    id = Column(Integer, primary_key=True, index=True)
    cliente_id = Column(Integer, ForeignKey("clientes.id"))
    
    # Datos extraídos del ticket por el Bot
    monto = Column(Float, nullable=False)
    folio_banco = Column(String(100), unique=True, nullable=False) # 👈 ESTO EVITA EL FRAUDE
    banco_emisor = Column(String(50), nullable=True)
    fecha_pago_banco = Column(String(50), nullable=True)
    
    # Datos de control
    whatsapp_remitente = Column(String(20), nullable=True)
    fecha_registro = Column(DateTime, default=datetime.now)

    # Relación para saber de quién es el dinero
    # cliente = relationship("ClienteModel", back_populates="pagos_auto")