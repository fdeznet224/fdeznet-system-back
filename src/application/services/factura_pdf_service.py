"""PDF de una factura con la marca y los datos de pago: lo usan finanzas y el portal del cliente."""

from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.helpers.pdf_generator import generar_factura_pdf
from src.application.services.comprobante_service import personalizar_datos_pago
from src.infrastructure.models import ConfiguracionSistema, PlantillaMensajeModel


async def pdf_de_factura(db: AsyncSession, factura) -> bytes:
    """`factura` con su cliente y conceptos ya cargados."""
    marca = await db.get(ConfiguracionSistema, 1)
    plantilla_pago = (
        await db.execute(
            select(PlantillaMensajeModel.texto).where(
                PlantillaMensajeModel.tipo == "datos_pago",
                PlantillaMensajeModel.activo.is_(True),
            )
        )
    ).scalar_one_or_none()
    hoy = date.today()
    limite = factura.fecha_promesa_pago if (factura.es_promesa_activa and factura.fecha_promesa_pago) else factura.fecha_vencimiento
    return generar_factura_pdf(
        factura,
        factura.cliente,
        marca,
        vencida=bool(limite and limite < hoy),
        datos_pago=personalizar_datos_pago(plantilla_pago, factura.cliente.cedula) if plantilla_pago else None,
    )


def nombre_pdf(factura) -> str:
    return f"factura-{str(factura.id).zfill(6)}.pdf"
