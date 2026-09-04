import enum
from sqlalchemy import (
    Column,
    Date,
    Integer,
    String,
    Boolean,
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Float,
    Text,
    Table,
    Index,
    Numeric,
    UniqueConstraint,
)
from sqlalchemy.sql import func
from sqlalchemy import text
from sqlalchemy.orm import relationship
from .database import Base
from datetime import date, datetime
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


class TipoFacturacion(str, enum.Enum):
    prepago = "prepago"
    postpago = "postpago"


class CicloFacturacion(str, enum.Enum):
    calendario = "calendario"
    aniversario = "aniversario"

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
    movimientos = relationship(
        "HistorialEquipoModel",
        back_populates="onu",
        order_by="HistorialEquipoModel.fecha.desc()",
    )

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
    created_at = Column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
    )

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
    precio = Column(Numeric(12, 2), nullable=False, default=0)
    
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
    puertos = relationship(
        "PuertoNapModel",
        back_populates="caja_nap",
        cascade="all, delete-orphan",
        order_by="PuertoNapModel.numero",
    )


class OLTModel(Base):
    __tablename__ = "olts"
    
    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String(100), nullable=False)        # Ej: "OLT San Lucas"
    ip = Column(String(50), nullable=False, unique=True)
    comunidad = Column(String(50), default="public")
    tecnologia = Column(String(20), nullable=False)     # "GPON" o "EPON"
    modelo = Column(String(50))                         # "V1600GS", "V1601E02-DP", "HIOSO"

    # VSOL API JSON / Web API
    tipo_integracion = Column(
        String(20),
        default="snmp",
        server_default="snmp",
    )  # snmp | vsol_api | auto
    api_enabled = Column(Boolean, default=False, server_default="0")
    api_protocol = Column(String(10), default="https", server_default="https")
    api_port = Column(Integer, default=443, server_default="443")
    api_user = Column(String(100), nullable=True)
    api_password = Column(String(255), nullable=True)
    api_verify_ssl = Column(Boolean, default=False, server_default="0")
    
    router_id = Column(Integer, ForeignKey("routers.id"), nullable=True) # A qué MikroTik está conectada
    is_active = Column(Boolean, default=True)
    created_at = Column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
    )

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
    impuesto = Column(Numeric(5, 2), default=0, server_default=text("0.00"))
    recordatorio_whatsapp = Column(Boolean, default=True)
    aviso_factura = Column(String(50), default='whatsapp') 
    
    clientes = relationship("ClienteModel", back_populates="plantilla")


class PoliticaCobranzaModel(Base):
    __tablename__ = "politicas_cobranza"

    id = Column(Integer, primary_key=True)
    nombre = Column(String(100), nullable=False, unique=True)
    tipo_cliente = Column(String(30), nullable=False, unique=True)
    dias_max_promesa = Column(Integer, nullable=False, default=7, server_default="7")
    max_promesas_activas = Column(Integer, nullable=False, default=1, server_default="1")
    max_incumplidas_90_dias = Column(Integer, nullable=False, default=2, server_default="2")
    permite_reconexion = Column(Boolean, nullable=False, default=True, server_default="1")
    activa = Column(Boolean, nullable=False, default=True, server_default="1")
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )

    clientes = relationship("ClienteModel", back_populates="politica_cobranza")


class ClienteModel(Base):
    __tablename__ = "clientes"
    __table_args__ = (
        UniqueConstraint("onu_id", name="uq_clientes_onu_id"),
        UniqueConstraint(
            "caja_nap_id",
            "puerto_nap",
            name="uq_clientes_nap_puerto",
        ),
    )
    
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
    politica_cobranza_id = Column(
        Integer,
        ForeignKey("politicas_cobranza.id"),
        nullable=True,
        index=True,
    )
    tipo_cliente = Column(
        String(30),
        nullable=False,
        default="residencial",
        server_default="residencial",
    )
    olt_id = Column(Integer, ForeignKey("olts.id"), nullable=True)

    # 👇👇👇 CAMPOS FTTH 👇👇👇
    caja_nap_id = Column(Integer, ForeignKey("cajas_nap.id"), nullable=True)
    puerto_nap = Column(Integer, nullable=True)
    tecnico_id = Column(Integer, ForeignKey("usuarios.id"), nullable=True)
    onu_id = Column(Integer, ForeignKey('inventario_onus.id'), nullable=True)
    onu_asignada = relationship("InventarioONUModel", back_populates="cliente")
    puerto_ftth = relationship(
        "PuertoNapModel",
        back_populates="cliente",
        uselist=False,
        foreign_keys="PuertoNapModel.cliente_id",
    )
    ordenes_servicio = relationship(
        "OrdenServicioModel",
        back_populates="cliente",
        foreign_keys="OrdenServicioModel.cliente_id",
    )
    
    # ... (Tus relaciones se mantienen igual)
    tecnico = relationship("UsuarioModel", foreign_keys=[tecnico_id])
    router = relationship("RouterModel", back_populates="clientes")
    plan = relationship("PlanModel", back_populates="clientes")
    zona = relationship("ZonaModel", back_populates="clientes")
    red = relationship("RedModel", back_populates="clientes")
    plantilla = relationship("PlantillaFacturacionModel", back_populates="clientes")
    politica_cobranza = relationship(
        "PoliticaCobranzaModel",
        back_populates="clientes",
    )
    caja_nap = relationship("CajaNapModel", back_populates="clientes")
    olt = relationship("OLTModel", back_populates="clientes")
    
    
    estado = Column(String(50), default="activo")
    created_at = Column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
    )
    proxima_factura = Column(Date) 
    saldo_a_favor = Column(
        Numeric(12, 2),
        nullable=False,
        default=0,
        server_default=text("0.00"),
    )

    facturas = relationship("FacturaModel", back_populates="cliente")
    pagos = relationship("PagoModel", back_populates="cliente")
    servicios = relationship("ServicioModel",back_populates="cliente",)
    bajas_servicio = relationship(
        "BajaServicioModel",
        back_populates="cliente",
        order_by="BajaServicioModel.solicitada_en.desc()",
    )

    latitud = Column(Float, nullable=True)
    longitud = Column(Float, nullable=True)
    is_online = Column(Boolean, default=False)
    ultimo_cambio_estado = Column(DateTime, default=func.now(), onupdate=func.now())


# ==========================================
# 3.1 ÓRDENES TÉCNICAS Y CONTROL FTTH
# ==========================================
class OrdenServicioModel(Base):
    __tablename__ = "ordenes_servicio"
    __table_args__ = (
        Index("ix_ordenes_estado_programada", "estado", "fecha_programada"),
        Index("ix_ordenes_tecnico_estado", "tecnico_id", "estado"),
    )

    id = Column(Integer, primary_key=True)
    tipo = Column(String(30), nullable=False)
    cliente_id = Column(
        Integer,
        ForeignKey("clientes.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    servicio_id = Column(
        Integer,
        ForeignKey("servicios.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Sugerencia de infraestructura para la orden. No reserva el puerto;
    # la asignación definitiva ocurre al confirmar la instalación.
    caja_nap_sugerida_id = Column(
        Integer,
        ForeignKey("cajas_nap.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    puerto_nap_sugerido = Column(Integer, nullable=True)
    prospecto_nombre = Column(String(150), nullable=True)
    prospecto_telefono = Column(String(20), nullable=True)
    prospecto_direccion = Column(String(255), nullable=True)
    tecnico_id = Column(
        Integer,
        ForeignKey("usuarios.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    creado_por_id = Column(
        Integer,
        ForeignKey("usuarios.id", ondelete="SET NULL"),
        nullable=True,
    )
    prioridad = Column(String(20), nullable=False, default="normal", server_default="normal")
    estado = Column(String(30), nullable=False, default="pendiente", server_default="pendiente")
    fecha_programada = Column(DateTime, nullable=True)
    fecha_inicio = Column(DateTime, nullable=True)
    fecha_finalizacion = Column(DateTime, nullable=True)
    fecha_cancelacion = Column(DateTime, nullable=True)
    motivo = Column(String(100), nullable=True)
    categoria_soporte = Column(String(30), nullable=True, index=True)
    canal_reporte = Column(
        String(20),
        nullable=False,
        default="panel",
        server_default="panel",
    )
    descripcion = Column(Text, nullable=True)
    diagnostico = Column(Text, nullable=True)
    solucion = Column(Text, nullable=True)
    tiempo_primera_respuesta_minutos = Column(Integer, nullable=True)
    tiempo_resolucion_minutos = Column(Integer, nullable=True)
    conformidad_cliente = Column(Boolean, nullable=False, default=False, server_default="0")
    version = Column(Integer, nullable=False, default=1, server_default="1")
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=func.now(),
    )

    cliente = relationship(
        "ClienteModel",
        back_populates="ordenes_servicio",
        foreign_keys=[cliente_id],
    )
    servicio = relationship(
        "ServicioModel",
        back_populates="ordenes",
        foreign_keys=[servicio_id],
    )
    tecnico = relationship("UsuarioModel", foreign_keys=[tecnico_id])
    creado_por = relationship("UsuarioModel", foreign_keys=[creado_por_id])
    historial = relationship(
        "HistorialEstadoOrdenModel",
        back_populates="orden",
        cascade="all, delete-orphan",
        order_by="HistorialEstadoOrdenModel.fecha",
    )
    evidencias = relationship(
        "EvidenciaOrdenModel",
        back_populates="orden",
        cascade="all, delete-orphan",
        order_by="EvidenciaOrdenModel.fecha",
    )
    materiales = relationship(
        "MaterialOrdenModel",
        back_populates="orden",
        cascade="all, delete-orphan",
    )
    diagnosticos_soporte = relationship(
        "DiagnosticoSoporteModel",
        back_populates="orden",
        cascade="all, delete-orphan",
        order_by="DiagnosticoSoporteModel.fecha.desc()",
    )


class DiagnosticoSoporteModel(Base):
    __tablename__ = "diagnosticos_soporte"
    __table_args__ = (
        Index("ix_diagnosticos_orden_fecha", "orden_id", "fecha"),
        Index("ix_diagnosticos_cliente_fecha", "cliente_id", "fecha"),
    )

    id = Column(Integer, primary_key=True)
    orden_id = Column(
        Integer,
        ForeignKey("ordenes_servicio.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    cliente_id = Column(
        Integer,
        ForeignKey("clientes.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    servicio_id = Column(
        Integer,
        ForeignKey("servicios.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    ejecutado_por_id = Column(
        Integer,
        ForeignKey("usuarios.id", ondelete="SET NULL"),
        nullable=True,
    )
    resultado = Column(String(20), nullable=False)
    codigo_sugerencia = Column(String(50), nullable=False)
    sugerencia = Column(Text, nullable=False)

    mikrotik_disponible = Column(Boolean, nullable=False, default=False, server_default="0")
    pppoe_online = Column(Boolean, nullable=True)
    ip_actual = Column(String(45), nullable=True)
    uptime = Column(String(50), nullable=True)
    mac_reportada = Column(String(50), nullable=True)
    ping_estado = Column(String(20), nullable=True)
    perdida_paquetes_porcentaje = Column(Numeric(5, 2), nullable=True)
    trafico_subida_bps = Column(BigInteger, nullable=True)
    trafico_bajada_bps = Column(BigInteger, nullable=True)

    olt_disponible = Column(Boolean, nullable=False, default=False, server_default="0")
    onu_online = Column(Boolean, nullable=True)
    potencia_rx_dbm = Column(Numeric(6, 2), nullable=True)
    potencia_tx_dbm = Column(Numeric(6, 2), nullable=True)
    origen_olt = Column(String(20), nullable=True)

    errores = Column(Text, nullable=True)
    datos_crudos = Column(Text, nullable=True)
    fecha = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )

    orden = relationship("OrdenServicioModel", back_populates="diagnosticos_soporte")
    cliente = relationship("ClienteModel")
    servicio = relationship("ServicioModel")
    ejecutado_por = relationship("UsuarioModel")


class HistorialEstadoOrdenModel(Base):
    __tablename__ = "historial_estados_orden"
    __table_args__ = (
        Index("ix_historial_orden_fecha", "orden_id", "fecha"),
    )

    id = Column(Integer, primary_key=True)
    orden_id = Column(
        Integer,
        ForeignKey("ordenes_servicio.id", ondelete="CASCADE"),
        nullable=False,
    )
    usuario_id = Column(
        Integer,
        ForeignKey("usuarios.id", ondelete="SET NULL"),
        nullable=True,
    )
    estado_anterior = Column(String(30), nullable=True)
    estado_nuevo = Column(String(30), nullable=False)
    comentario = Column(Text, nullable=True)
    fecha = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )

    orden = relationship("OrdenServicioModel", back_populates="historial")
    usuario = relationship("UsuarioModel")


class EvidenciaOrdenModel(Base):
    __tablename__ = "evidencias_orden"

    id = Column(Integer, primary_key=True)
    orden_id = Column(
        Integer,
        ForeignKey("ordenes_servicio.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    usuario_id = Column(
        Integer,
        ForeignKey("usuarios.id", ondelete="SET NULL"),
        nullable=True,
    )
    tipo = Column(String(20), nullable=False, default="foto", server_default="foto")
    nombre_original = Column(String(255), nullable=False)
    ruta_archivo = Column(String(500), nullable=False)
    mime_type = Column(String(100), nullable=False)
    tamano_bytes = Column(Integer, nullable=False)
    comentario = Column(String(500), nullable=True)
    fecha = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )

    orden = relationship("OrdenServicioModel", back_populates="evidencias")
    usuario = relationship("UsuarioModel")


class MaterialOrdenModel(Base):
    __tablename__ = "materiales_orden"

    id = Column(Integer, primary_key=True)
    orden_id = Column(
        Integer,
        ForeignKey("ordenes_servicio.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    descripcion = Column(String(150), nullable=False)
    cantidad = Column(Numeric(10, 2), nullable=False)
    unidad = Column(String(30), nullable=False, default="pieza", server_default="pieza")
    observaciones = Column(String(500), nullable=True)

    orden = relationship("OrdenServicioModel", back_populates="materiales")


class PuertoNapModel(Base):
    __tablename__ = "puertos_nap"
    __table_args__ = (
        UniqueConstraint("caja_nap_id", "numero", name="uq_puertos_nap_caja_numero"),
        UniqueConstraint("servicio_id", name="uq_puertos_nap_servicio_id"),
        Index("ix_puertos_nap_caja_estado", "caja_nap_id", "estado"),
    )

    id = Column(Integer, primary_key=True)
    caja_nap_id = Column(
        Integer,
        ForeignKey("cajas_nap.id", ondelete="CASCADE"),
        nullable=False,
    )
    numero = Column(Integer, nullable=False)
    estado = Column(String(20), nullable=False, default="libre", server_default="libre")
    cliente_id = Column(
        Integer,
        ForeignKey("clientes.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    servicio_id = Column(
        Integer,
        ForeignKey("servicios.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    orden_id = Column(
        Integer,
        ForeignKey("ordenes_servicio.id", ondelete="SET NULL"),
        nullable=True,
    )
    potencia_instalacion_dbm = Column(Numeric(6, 2), nullable=True)
    observaciones = Column(String(500), nullable=True)
    actualizado_por_id = Column(
        Integer,
        ForeignKey("usuarios.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=func.now(),
    )

    caja_nap = relationship("CajaNapModel", back_populates="puertos")
    cliente = relationship(
        "ClienteModel",
        back_populates="puerto_ftth",
        foreign_keys=[cliente_id],
    )
    servicio = relationship(
        "ServicioModel",
        back_populates="puerto_ftth",
        foreign_keys=[servicio_id],
    )
    orden = relationship("OrdenServicioModel")
    actualizado_por = relationship("UsuarioModel")


class HistorialEquipoModel(Base):
    __tablename__ = "historial_equipos"
    __table_args__ = (
        Index("ix_historial_equipo_fecha", "onu_id", "fecha"),
    )

    id = Column(Integer, primary_key=True)
    onu_id = Column(
        Integer,
        ForeignKey("inventario_onus.id", ondelete="RESTRICT"),
        nullable=False,
    )
    cliente_id = Column(
        Integer,
        ForeignKey("clientes.id", ondelete="SET NULL"),
        nullable=True,
    )
    servicio_id = Column(
        Integer,
        ForeignKey("servicios.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    tecnico_id = Column(
        Integer,
        ForeignKey("usuarios.id", ondelete="SET NULL"),
        nullable=True,
    )
    orden_id = Column(
        Integer,
        ForeignKey("ordenes_servicio.id", ondelete="SET NULL"),
        nullable=True,
    )
    tipo_movimiento = Column(String(30), nullable=False)
    estado_anterior = Column(String(50), nullable=True)
    estado_nuevo = Column(String(50), nullable=False)
    condicion = Column(String(30), nullable=True)
    motivo = Column(String(500), nullable=True)
    potencia_optica_dbm = Column(Numeric(6, 2), nullable=True)
    fecha = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )

    onu = relationship("InventarioONUModel", back_populates="movimientos")
    cliente = relationship("ClienteModel")
    servicio = relationship("ServicioModel")
    tecnico = relationship("UsuarioModel")
    orden = relationship("OrdenServicioModel")


class LecturaOpticaModel(Base):
    __tablename__ = "lecturas_opticas"
    __table_args__ = (
        Index("ix_lecturas_opticas_cliente_fecha", "cliente_id", "fecha"),
        Index("ix_lecturas_opticas_onu_fecha", "onu_id", "fecha"),
    )

    id = Column(Integer, primary_key=True)
    cliente_id = Column(
        Integer,
        ForeignKey("clientes.id", ondelete="CASCADE"),
        nullable=False,
    )
    servicio_id = Column(
        Integer,
        ForeignKey("servicios.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    onu_id = Column(
        Integer,
        ForeignKey("inventario_onus.id", ondelete="SET NULL"),
        nullable=True,
    )
    orden_id = Column(
        Integer,
        ForeignKey("ordenes_servicio.id", ondelete="SET NULL"),
        nullable=True,
    )
    tecnico_id = Column(
        Integer,
        ForeignKey("usuarios.id", ondelete="SET NULL"),
        nullable=True,
    )
    potencia_rx_dbm = Column(Numeric(6, 2), nullable=False)
    potencia_tx_dbm = Column(Numeric(6, 2), nullable=True)
    origen = Column(String(20), nullable=False, default="manual", server_default="manual")
    observaciones = Column(String(500), nullable=True)
    fecha = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )

    cliente = relationship("ClienteModel")
    servicio = relationship("ServicioModel")
    onu = relationship("InventarioONUModel")
    orden = relationship("OrdenServicioModel")
    tecnico = relationship("UsuarioModel")


class FacturaModel(Base):
    __tablename__ = "facturas"
    __table_args__ = (
        UniqueConstraint(
            "servicio_id",
            "periodo_desde",
            "periodo_hasta",
            name="uq_factura_servicio_periodo",
        ),
    )
    id = Column(Integer, primary_key=True, index=True)
    cliente_id = Column(Integer, ForeignKey("clientes.id"))
    servicio_id = Column(
    Integer,ForeignKey("servicios.id"),nullable=True,index=True,)
    
    plan_snapshot = Column(String(150))
    detalles = Column(Text)
    monto = Column(Numeric(12, 2), nullable=False, default=0)
    impuesto = Column(
        Numeric(12, 2),
        nullable=False,
        default=0,
        server_default=text("0.00"),
    )
    total = Column(Numeric(12, 2), nullable=False, default=0)
    saldo_pendiente = Column(
        Numeric(12, 2),
        nullable=False,
        default=0,
        server_default=text("0.00"),
    )
    descuento_total = Column(
        Numeric(12, 2),
        nullable=False,
        default=0,
        server_default=text("0.00"),
    )
    
    fecha_emision = Column(Date)       
    fecha_vencimiento = Column(Date)
    fecha_limite_corte = Column(Date)
    periodo_desde = Column(Date, nullable=True)
    periodo_hasta = Column(Date, nullable=True)
    dias_facturados = Column(Integer, nullable=True)
    dias_periodo = Column(Integer, nullable=True)
    precio_mensual_snapshot = Column(Numeric(12, 2), nullable=True)
    precio_diario = Column(Numeric(12, 4), nullable=True)
    es_prorrateada = Column(
        Boolean,
        default=False,
        server_default="0",
        nullable=False,
    )
    tipo_facturacion_snapshot = Column(String(20), nullable=True)
    ciclo_facturacion_snapshot = Column(String(20), nullable=True)

    fecha_pago_real = Column(DateTime, nullable=True)
    mes_correspondiente = Column(String(100)) 
    estado = Column(String(20), default="pendiente")
    
    fecha_promesa_pago = Column(Date, nullable=True)
    es_promesa_activa = Column(Boolean, default=False)

    # Clasificación comercial de la factura
    tipo_factura = Column(String(30), default="mensual", server_default="mensual", nullable=False)
    concepto = Column(String(150), nullable=True)
    descripcion = Column(String(500), nullable=True)
    afecta_corte = Column(Boolean, default=True, server_default="1", nullable=False)
    creada_manual = Column(Boolean, default=False, server_default="0", nullable=False)
    motivo_anulacion = Column(String(500), nullable=True)
    anulada_por_id = Column(
        Integer,
        ForeignKey("usuarios.id", ondelete="SET NULL"),
        nullable=True,
    )
    fecha_anulacion = Column(DateTime(timezone=True), nullable=True)
    saldo_antes_anulacion = Column(Numeric(12, 2), nullable=True)
    monto_servicio_original = Column(Numeric(12, 2), nullable=True)
    impuesto_servicio_original = Column(Numeric(12, 2), nullable=True)
    cargos_adicionales_total = Column(
        Numeric(12, 2), nullable=False, default=0, server_default=text("0.00")
    )
    dias_con_servicio = Column(Integer, nullable=True)
    dias_sin_servicio = Column(Integer, nullable=True)
    ajuste_suspension = Column(
        Numeric(12, 2), nullable=False, default=0, server_default=text("0.00")
    )
    
    cliente = relationship("ClienteModel", back_populates="facturas")
    servicio = relationship("ServicioModel",back_populates="facturas",)
    pagos = relationship("PagoModel", back_populates="factura")
    descuentos = relationship(
        "DescuentoFacturaModel",
        back_populates="factura",
        order_by="DescuentoFacturaModel.fecha.desc()",
    )
    promesas = relationship(
        "PromesaPagoHistorialModel",
        back_populates="factura",
        order_by="PromesaPagoHistorialModel.created_at.desc()",
    )
    anulada_por = relationship(
        "UsuarioModel",
        foreign_keys=[anulada_por_id],
    )
    conceptos = relationship(
        "FacturaConceptoModel",
        back_populates="factura",
        order_by="FacturaConceptoModel.id",
    )


class FacturaConceptoModel(Base):
    """Renglón cobrable que puede esperar a la próxima factura mensual."""

    __tablename__ = "factura_conceptos"

    id = Column(Integer, primary_key=True, index=True)
    factura_id = Column(
        Integer,
        ForeignKey("facturas.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    cliente_id = Column(Integer, ForeignKey("clientes.id"), nullable=False, index=True)
    servicio_id = Column(Integer, ForeignKey("servicios.id"), nullable=True, index=True)
    tipo = Column(String(30), nullable=False, default="cargo", server_default="cargo")
    concepto = Column(String(150), nullable=False)
    descripcion = Column(String(500), nullable=True)
    monto_original = Column(Numeric(12, 2), nullable=False)
    saldo_pendiente = Column(Numeric(12, 2), nullable=False)
    estado = Column(String(20), nullable=False, default="pendiente", server_default="pendiente")
    afecta_corte = Column(Boolean, nullable=False, default=False, server_default="0")
    fecha_cargo = Column(Date, nullable=False, default=date.today)
    numero_cuota = Column(Integer, nullable=True)
    total_cuotas = Column(Integer, nullable=True)
    cargo_origen_id = Column(Integer, nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.now)

    factura = relationship("FacturaModel", back_populates="conceptos")
    cliente = relationship("ClienteModel")
    servicio = relationship("ServicioModel")
    aplicaciones_pago = relationship("PagoConceptoModel", back_populates="concepto")


class PagoConceptoModel(Base):
    __tablename__ = "pago_conceptos"
    id = Column(Integer, primary_key=True)
    pago_id = Column(Integer, ForeignKey("pagos.id", ondelete="CASCADE"), nullable=False, index=True)
    concepto_id = Column(Integer, ForeignKey("factura_conceptos.id", ondelete="CASCADE"), nullable=False, index=True)
    monto_aplicado = Column(Numeric(12, 2), nullable=False)
    created_at = Column(DateTime, default=datetime.now)
    pago = relationship("PagoModel", back_populates="aplicaciones_conceptos")
    concepto = relationship("FacturaConceptoModel", back_populates="aplicaciones_pago")


class SuspensionFacturacionModel(Base):
    __tablename__ = "suspensiones_facturacion"
    __table_args__ = (
        Index(
            "ix_suspensiones_facturacion_servicio_fechas",
            "servicio_id",
            "fecha_inicio",
            "fecha_fin",
        ),
    )

    id = Column(Integer, primary_key=True)
    servicio_id = Column(
        Integer,
        ForeignKey("servicios.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    factura_origen_id = Column(
        Integer,
        ForeignKey("facturas.id", ondelete="SET NULL"),
        nullable=True,
    )
    fecha_inicio = Column(Date, nullable=False)
    fecha_fin = Column(Date, nullable=True)
    motivo_inicio = Column(String(100), nullable=False, default="falta_pago")
    motivo_fin = Column(String(100), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )

    servicio = relationship("ServicioModel")
    factura_origen = relationship("FacturaModel", foreign_keys=[factura_origen_id])



class ServicioModel(Base):
    __tablename__ = "servicios"

    __table_args__ = (
        Index(
            "ix_servicios_cliente_estado",
            "cliente_id",
            "estado",
        ),
        Index(
            "ix_servicios_proxima_facturacion",
            "proxima_facturacion",
        ),
        Index(
            "ix_servicios_router_estado",
            "router_id",
            "estado",
        ),
        UniqueConstraint(
            "onu_id",
            name="uq_servicios_onu_id",
        ),
        UniqueConstraint(
            "caja_nap_id",
            "puerto_nap",
            name="uq_servicios_nap_puerto",
        ),
    )

    id = Column(
        Integer,
        primary_key=True,
        index=True,
    )

    cliente_id = Column(
        Integer,
        ForeignKey("clientes.id"),
        nullable=False,
        index=True,
    )

    alias = Column(
        String(100),
        nullable=False,
        default="Principal",
        server_default="Principal",
    )
    direccion = Column(String(255), nullable=True)
    latitud = Column(Float, nullable=True)
    longitud = Column(Float, nullable=True)

    router_id = Column(
        Integer,
        ForeignKey("routers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    zona_id = Column(
        Integer,
        ForeignKey("zonas.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    red_id = Column(
        Integer,
        ForeignKey("redes.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    olt_id = Column(
        Integer,
        ForeignKey("olts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    caja_nap_id = Column(
        Integer,
        ForeignKey("cajas_nap.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    puerto_nap = Column(Integer, nullable=True)
    tecnico_id = Column(
        Integer,
        ForeignKey("usuarios.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    onu_id = Column(
        Integer,
        ForeignKey("inventario_onus.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    ip_asignada = Column(String(20), nullable=True, unique=True)
    mac_address = Column(String(20), nullable=True)
    user_pppoe = Column(String(50), nullable=True, index=True)
    pass_pppoe = Column(String(100), nullable=True)
    is_online = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
    )
    ultimo_cambio_estado = Column(
        DateTime,
        default=func.now(),
        onupdate=func.now(),
    )

    plan_id = Column(
        Integer,
        ForeignKey("planes.id"),
        nullable=True,
        index=True,
    )

    plantilla_id = Column(
        Integer,
        ForeignKey("plantillas_facturacion.id"),
        nullable=True,
        index=True,
    )

    tipo_facturacion = Column(
        Enum(
            TipoFacturacion,
            native_enum=False,
            length=20,
        ),
        nullable=False,
        default=TipoFacturacion.prepago,
        server_default=TipoFacturacion.prepago.value,
    )

    ciclo_facturacion = Column(
        Enum(
            CicloFacturacion,
            native_enum=False,
            length=20,
        ),
        nullable=False,
        default=CicloFacturacion.calendario,
        server_default=CicloFacturacion.calendario.value,
    )

    fecha_instalacion = Column(
        Date,
        nullable=True,
    )

    fecha_activacion = Column(
        Date,
        nullable=True,
    )

    fecha_inicio_servicio = Column(
        Date,
        nullable=True,
    )

    fecha_fin_periodo_gratis = Column(
        Date,
        nullable=True,
    )

    fecha_inicio_cobro = Column(
        Date,
        nullable=True,
    )

    proxima_facturacion = Column(
        Date,
        nullable=True,
    )

    dia_vencimiento = Column(
        Integer,
        nullable=True,
    )

    dias_tolerancia = Column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )

    meses_gratis = Column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )

    politica_prorrateo = Column(
        String(30),
        nullable=False,
        default="dias_reales_mes",
        server_default="dias_reales_mes",
    )

    # Intervalo abierto cuando el corte por falta de pago fue confirmado.
    # Se usa para no cobrar los días en los que no se prestó el servicio.
    fecha_suspension_facturacion = Column(Date, nullable=True)
    fecha_ultima_reactivacion = Column(Date, nullable=True)

    estado = Column(
        String(30),
        nullable=False,
        default="pendiente_instalacion",
        server_default="pendiente_instalacion",
    )

    created_at = Column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    updated_at = Column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=func.now(),
        nullable=False,
    )

    cliente = relationship(
        "ClienteModel",
        back_populates="servicios",
    )

    plan = relationship(
        "PlanModel",
    )

    plantilla = relationship(
        "PlantillaFacturacionModel",
    )

    router = relationship("RouterModel")
    zona = relationship("ZonaModel")
    red = relationship("RedModel")
    olt = relationship("OLTModel")
    caja_nap = relationship("CajaNapModel")
    tecnico = relationship("UsuarioModel", foreign_keys=[tecnico_id])
    onu = relationship("InventarioONUModel", foreign_keys=[onu_id])
    puerto_ftth = relationship(
        "PuertoNapModel",
        back_populates="servicio",
        uselist=False,
        foreign_keys="PuertoNapModel.servicio_id",
    )
    ordenes = relationship(
        "OrdenServicioModel",
        back_populates="servicio",
        foreign_keys="OrdenServicioModel.servicio_id",
    )

    facturas = relationship(
        "FacturaModel",
        back_populates="servicio",
    )


class BajaServicioModel(Base):
    __tablename__ = "bajas_servicio"
    __table_args__ = (
        Index("ix_bajas_cliente_estado", "cliente_id", "estado"),
        Index("ix_bajas_tecnico_estado", "tecnico_id", "estado"),
        UniqueConstraint(
            "orden_retiro_id",
            name="uq_bajas_servicio_orden_retiro",
        ),
    )

    id = Column(Integer, primary_key=True)
    cliente_id = Column(
        Integer,
        ForeignKey("clientes.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    servicio_id = Column(
        Integer,
        ForeignKey("servicios.id", ondelete="SET NULL"),
        nullable=True,
    )
    orden_retiro_id = Column(
        Integer,
        ForeignKey("ordenes_servicio.id", ondelete="SET NULL"),
        nullable=True,
    )
    onu_id = Column(
        Integer,
        ForeignKey("inventario_onus.id", ondelete="SET NULL"),
        nullable=True,
    )
    solicitada_por_id = Column(
        Integer,
        ForeignKey("usuarios.id", ondelete="SET NULL"),
        nullable=True,
    )
    tecnico_id = Column(
        Integer,
        ForeignKey("usuarios.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    estado = Column(
        String(30),
        nullable=False,
        default="pendiente_retiro",
        server_default="pendiente_retiro",
    )
    motivo = Column(String(500), nullable=False)
    observaciones = Column(Text, nullable=True)
    condicion_equipo = Column(String(30), nullable=True)
    mikrotik_estado = Column(
        String(20),
        nullable=False,
        default="pendiente",
        server_default="pendiente",
    )
    mikrotik_error = Column(Text, nullable=True)

    ip_snapshot = Column(String(20), nullable=True)
    caja_nap_id_snapshot = Column(Integer, nullable=True)
    puerto_nap_snapshot = Column(Integer, nullable=True)
    servicio_estado_snapshot = Column(String(30), nullable=True)
    proxima_facturacion_snapshot = Column(Date, nullable=True)

    solicitada_en = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    recuperada_en = Column(DateTime(timezone=True), nullable=True)
    cancelada_en = Column(DateTime(timezone=True), nullable=True)

    cliente = relationship("ClienteModel", back_populates="bajas_servicio")
    servicio = relationship("ServicioModel")
    orden_retiro = relationship("OrdenServicioModel")
    onu = relationship("InventarioONUModel")
    solicitada_por = relationship(
        "UsuarioModel",
        foreign_keys=[solicitada_por_id],
    )
    tecnico = relationship("UsuarioModel", foreign_keys=[tecnico_id])



class PagoModel(Base):
    __tablename__ = "pagos"
    id = Column(Integer, primary_key=True, index=True)
    cliente_id = Column(Integer, ForeignKey("clientes.id"))
    usuario_id = Column(Integer, ForeignKey("usuarios.id"))
    factura_id = Column(Integer, ForeignKey("facturas.id"), nullable=True)
    monto_total = Column(Numeric(12, 2), nullable=False)
    monto_aplicado = Column(
        Numeric(12, 2),
        nullable=False,
        default=0,
        server_default=text("0.00"),
    )
    monto_saldo_favor = Column(
        Numeric(12, 2),
        nullable=False,
        default=0,
        server_default=text("0.00"),
    )
    monto_saldo_favor_usado = Column(
        Numeric(12, 2),
        nullable=False,
        default=0,
        server_default=text("0.00"),
    )
    saldo_anterior = Column(Numeric(12, 2), nullable=True)
    saldo_posterior = Column(Numeric(12, 2), nullable=True)
    metodo_pago = Column(String(50), default="efectivo")
    referencia = Column(String(100))
    clave_idempotencia = Column(String(100), nullable=True, unique=True)
    estado = Column(String(20), nullable=False, default="aplicado", server_default="aplicado")
    motivo_anulacion = Column(String(500), nullable=True)
    anulado_por_id = Column(
        Integer,
        ForeignKey("usuarios.id", ondelete="SET NULL"),
        nullable=True,
    )
    fecha_anulacion = Column(DateTime, nullable=True)
    fecha_pago = Column(DateTime, default=datetime.now)
    created_at = Column(DateTime, default=datetime.now) 
    
    cliente = relationship("ClienteModel", back_populates="pagos")
    factura = relationship("FacturaModel", back_populates="pagos")
    aplicaciones_conceptos = relationship("PagoConceptoModel", back_populates="pago")
    usuario = relationship(
        "UsuarioModel",
        back_populates="pagos",
        foreign_keys=[usuario_id],
    )
    anulado_por = relationship("UsuarioModel", foreign_keys=[anulado_por_id])
class DescuentoFacturaModel(Base):
    __tablename__ = "descuentos_factura"

    id = Column(Integer, primary_key=True)
    factura_id = Column(
        Integer,
        ForeignKey("facturas.id"),
        nullable=False,
        index=True,
    )
    aplicado_por_id = Column(
        Integer,
        ForeignKey("usuarios.id", ondelete="SET NULL"),
        nullable=True,
    )
    autorizado_por_id = Column(
        Integer,
        ForeignKey("usuarios.id", ondelete="SET NULL"),
        nullable=True,
    )
    monto = Column(Numeric(12, 2), nullable=False)
    saldo_anterior = Column(Numeric(12, 2), nullable=False)
    saldo_posterior = Column(Numeric(12, 2), nullable=False)
    motivo = Column(String(500), nullable=False)
    fecha = Column(
        DateTime,
        nullable=False,
        default=datetime.now,
        server_default=text("CURRENT_TIMESTAMP"),
    )

    factura = relationship("FacturaModel", back_populates="descuentos")
    aplicado_por = relationship("UsuarioModel", foreign_keys=[aplicado_por_id])
    autorizado_por = relationship("UsuarioModel", foreign_keys=[autorizado_por_id])


class PromesaPagoHistorialModel(Base):
    __tablename__ = "promesas_pago_historial"
    __table_args__ = (
        Index("ix_promesas_cliente_estado", "cliente_id", "estado"),
    )

    id = Column(Integer, primary_key=True)
    factura_id = Column(
        Integer,
        ForeignKey("facturas.id"),
        nullable=False,
        index=True,
    )
    cliente_id = Column(
        Integer,
        ForeignKey("clientes.id"),
        nullable=False,
        index=True,
    )
    usuario_id = Column(
        Integer,
        ForeignKey("usuarios.id", ondelete="SET NULL"),
        nullable=True,
    )
    fecha_prometida = Column(Date, nullable=False)
    fecha_anterior = Column(Date, nullable=True)
    estado = Column(String(20), nullable=False, default="activa", server_default="activa")
    notas = Column(String(500), nullable=True)
    created_at = Column(
        DateTime,
        nullable=False,
        default=datetime.now,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    resuelta_en = Column(DateTime, nullable=True)

    factura = relationship("FacturaModel", back_populates="promesas")
    cliente = relationship("ClienteModel")
    usuario = relationship("UsuarioModel")

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
    
    pagos = relationship(
        "PagoModel",
        back_populates="usuario",
        foreign_keys="PagoModel.usuario_id",
    )
    actividades = relationship(
        "LogActividadModel",
        back_populates="usuario",
        passive_deletes=True,
    )
    
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


class OperacionSincronizacionModel(Base):
    __tablename__ = "operaciones_sincronizacion"
    __table_args__ = (
        Index(
            "ix_operaciones_sincronizacion_usuario_fecha",
            "usuario_id",
            "aplicado_en",
        ),
    )

    id = Column(String(36), primary_key=True)
    usuario_id = Column(
        Integer,
        ForeignKey("usuarios.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tipo = Column(String(40), nullable=False)
    payload_hash = Column(String(64), nullable=False)
    respuesta = Column(Text, nullable=False)
    creado_cliente = Column(DateTime(timezone=True), nullable=True)
    aplicado_en = Column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    usuario = relationship("UsuarioModel")


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
# 5. AUDITORÍA Y LOGS DE CRONJOBS
# ==========================================
class LogActividadModel(Base):
    __tablename__ = "logs_actividad"
    __table_args__ = (
        Index("ix_logs_actividad_fecha", "fecha"),
        Index("ix_logs_actividad_usuario_fecha", "usuario_id", "fecha"),
        Index("ix_logs_actividad_accion", "accion"),
    )

    id = Column(Integer, primary_key=True)
    usuario_id = Column(
        Integer,
        ForeignKey("usuarios.id", ondelete="SET NULL"),
        nullable=True,
    )
    usuario_nombre = Column(String(50), nullable=True)
    accion = Column(String(100), nullable=False)
    metodo = Column(String(10), nullable=False)
    ruta = Column(String(255), nullable=False)
    estado_http = Column(Integer, nullable=False)
    detalle = Column(Text, nullable=True)
    ip_cliente = Column(String(45), nullable=True)
    fecha = Column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    usuario = relationship("UsuarioModel", back_populates="actividades")


class LogCronjobModel(Base):
    __tablename__ = "logs_cronjobs"

    id = Column(Integer, primary_key=True, index=True)
    fecha = Column(DateTime, default=func.now()) 
    nivel = Column(String(20))
    origen = Column(String(50))
    mensaje = Column(Text)





class MensajeChatModel(Base):
    __tablename__ = "mensajes_chat"
    __table_args__ = (
        Index(
            "ix_mensajes_salida_estado_proximo",
            "direccion",
            "estado_envio",
            "proximo_intento_en",
        ),
        Index(
            "ix_mensajes_salida_fecha",
            "direccion",
            "fecha",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    cliente_id = Column(Integer, ForeignKey("clientes.id"), nullable=True) 
    telefono = Column(String(20), index=True) 
    direccion = Column(String(10)) 
    mensaje = Column(LONGTEXT)
    tipo_mensaje = Column(String(20), default="texto") 
    tipo_evento = Column(String(50), nullable=True)
    clave_dedupe = Column(String(150), nullable=True, unique=True)
    leido = Column(Boolean, default=False) 
    fecha = Column(DateTime, default=func.now())
    
    # 👇 NUEVAS COLUMNAS PARA RASTREAR ESTADO DE WHATSAPP 👇
    wa_id = Column(String(100), nullable=True, index=True) # ID interno del mensaje de WhatsApp
    ack = Column(Integer, default=0) # 0=Pendiente, 1=Enviado, 2=Entregado, 3=Visto
    estado_envio = Column(
        String(20),
        nullable=False,
        default="pendiente",
        server_default="pendiente",
    )
    # Intervalo individual para campañas y envíos masivos. NULL usa la
    # configuración global; persistirlo evita perder el ritmo al reiniciar.
    intervalo_salida = Column(Integer, nullable=True)
    intentos = Column(Integer, nullable=False, default=0, server_default="0")
    max_intentos = Column(
        Integer,
        nullable=False,
        default=5,
        server_default="5",
    )
    ultimo_error = Column(Text, nullable=True)
    ultima_tentativa_en = Column(DateTime(timezone=True), nullable=True)
    proximo_intento_en = Column(DateTime(timezone=True), nullable=True)
    bloqueado_hasta = Column(DateTime(timezone=True), nullable=True)
    enviado_en = Column(DateTime(timezone=True), nullable=True)
    entregado_en = Column(DateTime(timezone=True), nullable=True)
    leido_en = Column(DateTime(timezone=True), nullable=True)
    ruta_archivo = Column(String(500), nullable=True)
    lote_id = Column(String(36), nullable=True, index=True)
    reintentos_manuales = Column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    creado_por_id = Column(
        Integer,
        ForeignKey("usuarios.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    ultimo_reintento_por_id = Column(
        Integer,
        ForeignKey("usuarios.id", ondelete="SET NULL"),
        nullable=True,
    )

    cliente = relationship("ClienteModel", backref="historial_chat")
    creado_por = relationship(
        "UsuarioModel",
        foreign_keys=[creado_por_id],
    )
    ultimo_reintento_por = relationship(
        "UsuarioModel",
        foreign_keys=[ultimo_reintento_por_id],
    )



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
    monto = Column(Numeric(12, 2), nullable=False)
    folio_banco = Column(String(100), unique=True, nullable=False) # 👈 ESTO EVITA EL FRAUDE
    banco_emisor = Column(String(50), nullable=True)
    fecha_pago_banco = Column(String(50), nullable=True)
    
    # Datos de control
    whatsapp_remitente = Column(String(20), nullable=True)
    fecha_registro = Column(DateTime, default=datetime.now)

    # Relación para saber de quién es el dinero
    # cliente = relationship("ClienteModel", back_populates="pagos_auto")
