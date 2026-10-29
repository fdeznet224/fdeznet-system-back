"""Base del panel central (SQLite): los ISP alojados y sus pagos."""
import os
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

RUTA_BASE = os.getenv("PANEL_BASE", "/data/panel.db")
engine = create_engine(f"sqlite:///{RUTA_BASE}", connect_args={"check_same_thread": False})
Sesion = sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Isp(Base):
    __tablename__ = "isps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nombre: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(32), unique=True)
    dominio: Mapped[str] = mapped_column(String(255))
    contacto: Mapped[str | None] = mapped_column(String(120))
    telefono: Mapped[str | None] = mapped_column(String(30))
    plan: Mapped[str | None] = mapped_column(String(60))
    precio_mensual: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    dia_pago: Mapped[int] = mapped_column(Integer, default=1)
    pagado_hasta: Mapped[date | None] = mapped_column(Date)
    limite_clientes: Mapped[int | None] = mapped_column(Integer)
    limite_routers: Mapped[int | None] = mapped_column(Integer)
    whatsapp: Mapped[bool] = mapped_column(Boolean, default=True)
    suspendido: Mapped[bool] = mapped_column(Boolean, default=False)
    web_port: Mapped[int] = mapped_column(Integer, unique=True)
    wg_port: Mapped[int] = mapped_column(Integer, unique=True)
    version: Mapped[str] = mapped_column(String(30), default="local")
    notas: Mapped[str | None] = mapped_column(Text)
    creado: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    pagos: Mapped[list["Pago"]] = relationship(
        back_populates="isp", order_by="desc(Pago.id)", cascade="all, delete-orphan"
    )


class Pago(Base):
    __tablename__ = "pagos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    isp_id: Mapped[int] = mapped_column(ForeignKey("isps.id"))
    fecha: Mapped[date] = mapped_column(Date)
    monto: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    meses: Mapped[int] = mapped_column(Integer)
    cubre_hasta: Mapped[date] = mapped_column(Date)
    nota: Mapped[str | None] = mapped_column(String(255))

    isp: Mapped[Isp] = relationship(back_populates="pagos")


class Bitacora(Base):
    """Lo que se hizo desde el panel: altas, suspensiones, actualizaciones."""

    __tablename__ = "bitacora"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fecha: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    isp_id: Mapped[int | None] = mapped_column(ForeignKey("isps.id"))
    accion: Mapped[str] = mapped_column(String(60))
    detalle: Mapped[str | None] = mapped_column(Text)


class Respaldo(Base):
    """Cada respaldo que intentó el panel; los archivos viven en nube/respaldos/<slug>/."""

    __tablename__ = "respaldos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fecha: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    isp_id: Mapped[int] = mapped_column(ForeignKey("isps.id"))
    archivo: Mapped[str | None] = mapped_column(String(80))
    tamano: Mapped[int | None] = mapped_column(Integer)
    remoto: Mapped[str | None] = mapped_column(String(255))  # no, ok o el error de rclone
    error: Mapped[str | None] = mapped_column(Text)


class DriveIsp(Base):
    """Cuenta de Google Drive que conectó un ISP; la llave vive en secretos/drive/<slug>.conf."""

    __tablename__ = "drive"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    isp_id: Mapped[int] = mapped_column(ForeignKey("isps.id"), unique=True)
    correo: Mapped[str] = mapped_column(String(255))
    conectado: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


def crear_tablas() -> None:
    Base.metadata.create_all(engine)
