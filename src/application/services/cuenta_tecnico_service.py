"""Estado de cuenta explicado para el técnico.

Cuando un cliente pregunta en campo "¿por qué me cortaron?", el técnico ve
qué debe, desde cuándo está suspendido, su último pago y si tiene promesa.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import select

from src.infrastructure.models import (
    FacturaModel,
    PagoModel,
    PromesaPagoHistorialModel,
    ServicioModel,
)

ESTADOS_ADEUDO = {"pendiente", "vencida", "promesa"}


def _fecha(valor) -> Optional[str]:
    if not valor:
        return None
    if isinstance(valor, datetime):
        valor = valor.date()
    return valor.strftime("%d/%m/%Y")


def _dinero(valor) -> str:
    return f"${Decimal(valor or 0):,.2f}".replace(".00", "")


def explicar_estado(
    estado: str,
    total: Decimal,
    adeudos: list[dict],
    suspendido_desde: Optional[date],
    promesa: Optional[dict],
) -> str:
    """Una frase que el técnico puede decirle al cliente."""
    vencidos = [a for a in adeudos if a["vencido"]]
    if estado == "suspendido":
        desde = f" desde el {_fecha(suspendido_desde)}" if suspendido_desde else ""
        if total > 0:
            primero = vencidos[0] if vencidos else adeudos[0]
            return (
                f"Suspendido{desde} por adeudo de {_dinero(total)} "
                f"({primero['concepto']}, venció el {primero['vence']}). "
                "Se reactiva al pagar o con una promesa de pago."
            )
        return f"Suspendido{desde}, sin adeudo pendiente. Debe revisarlo administración."
    if estado == "cancelado":
        return "El servicio está dado de baja."
    if estado == "pendiente_instalacion":
        return "Instalación pendiente: el servicio aún no se activa."
    if promesa:
        return f"Activo con promesa de pago para el {promesa['fecha']} por {_dinero(total)}."
    if vencidos:
        return f"Activo, pero debe {_dinero(total)} vencido: puede suspenderse pronto."
    if total > 0:
        return f"Al corriente. Próximo pago de {_dinero(total)} vence el {adeudos[0]['vence']}."
    return "Al corriente, sin adeudos."


async def resumen_cuenta(db, cliente) -> dict:
    hoy = date.today()
    facturas = sorted(
        (f for f in cliente.facturas if f.estado in ESTADOS_ADEUDO and (f.saldo_pendiente or 0) > 0),
        key=lambda f: f.fecha_vencimiento or hoy,
    )
    adeudos = [
        {
            "concepto": f.concepto or f.mes_correspondiente or "Mensualidad",
            "monto": float(f.saldo_pendiente or 0),
            "vence": _fecha(f.fecha_vencimiento),
            "corte": _fecha(f.fecha_limite_corte),
            "vencido": bool(f.fecha_vencimiento and f.fecha_vencimiento < hoy) or f.estado == "vencida",
        }
        for f in facturas
    ]
    total = sum((Decimal(f.saldo_pendiente or 0) for f in facturas), Decimal("0"))

    servicio = (
        await db.execute(
            select(ServicioModel)
            .where(ServicioModel.cliente_id == cliente.id, ServicioModel.estado != "cancelado")
            .order_by(ServicioModel.id)
            .limit(1)
        )
    ).scalars().first()
    estado = servicio.estado if servicio else cliente.estado
    suspendido_desde = (
        (servicio.ultimo_cambio_estado if servicio else None) if estado == "suspendido" else None
    )

    pago = (
        await db.execute(
            select(PagoModel)
            .where(PagoModel.cliente_id == cliente.id, PagoModel.estado == "aplicado")
            .order_by(PagoModel.fecha_pago.desc())
            .limit(1)
        )
    ).scalars().first()
    promesa_activa = (
        await db.execute(
            select(PromesaPagoHistorialModel)
            .where(PromesaPagoHistorialModel.cliente_id == cliente.id, PromesaPagoHistorialModel.estado == "activa")
            .order_by(PromesaPagoHistorialModel.fecha_prometida.desc())
            .limit(1)
        )
    ).scalars().first()
    promesa = {"fecha": _fecha(promesa_activa.fecha_prometida)} if promesa_activa else None

    return {
        "estado_servicio": estado,
        "explicacion": explicar_estado(estado, total, adeudos, suspendido_desde, promesa),
        "adeudos": adeudos,
        "ultimo_pago": (
            {"fecha": _fecha(pago.fecha_pago), "monto": float(pago.monto_total or 0), "metodo": pago.metodo_pago}
            if pago else None
        ),
        "promesa": promesa,
        "suspendido_desde": _fecha(suspendido_desde),
    }
