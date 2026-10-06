"""Cómo le van a cobrar a un cliente recién activado, para que el técnico se lo
explique en el domicilio: mes gratis, prorrateo y mensualidad.

Sigue las reglas de facturación del sistema: el prorrateo abierto se suma a
la primera mensualidad normal (un solo recibo), en prepago la mensualidad
vence el día que empieza su periodo y en postpago al terminarlo.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from src.application.services.billing_calendar_service import BillingCalendarService

MESES = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
    "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)


@dataclass
class Prorrateo:
    total: Decimal
    desde: date
    hasta: date
    dias: int


def fecha_larga(fecha: date, referencia: date) -> str:
    """15 de noviembre (con el año solo si no es el de la activación)."""
    texto = f"{fecha.day} de {MESES[fecha.month - 1]}"
    return texto if fecha.year == referencia.year else f"{texto} de {fecha.year}"


def rango(desde: date, hasta: date, referencia: date) -> str:
    """del 5 al 14 de noviembre / del 5 de octubre al 4 de noviembre."""
    if (desde.year, desde.month) == (hasta.year, hasta.month):
        return f"del {desde.day} al {fecha_larga(hasta, referencia)}"
    return f"del {fecha_larga(desde, referencia)} al {fecha_larga(hasta, referencia)}"


def pesos(monto: Decimal) -> str:
    return f"${monto:,.2f}"


def resumen_cobro(
    *,
    activacion: date,
    meses_gratis: int,
    gratis_hasta: date | None,
    proxima_mensualidad: date,
    tipo_facturacion: str,
    precio_mensual: Decimal,
    impuesto_porcentaje: Decimal,
    dias_tolerancia: int,
    prorrateo: Prorrateo | None,
) -> dict:
    cal = BillingCalendarService
    mensualidad = cal.money(
        cal.to_decimal(precio_mensual)
        * (1 + cal.to_decimal(impuesto_porcentaje) / Decimal("100"))
    )
    fin_primer_mes = cal.add_months(proxima_mensualidad, 1) - timedelta(days=1)
    postpago = tipo_facturacion == "postpago"
    # Prepago: se paga al empezar el mes de servicio; postpago: al terminarlo.
    primer_pago = cal.add_months(proxima_mensualidad, 1) if postpago else proxima_mensualidad
    corte = primer_pago + timedelta(days=dias_tolerancia or 0)
    total_primer_pago = mensualidad + (prorrateo.total if prorrateo else Decimal("0"))

    def f(fecha: date) -> str:
        return fecha_larga(fecha, activacion)

    explicacion = []
    if meses_gratis and gratis_hasta:
        meses = f"{meses_gratis} mes gratis" if meses_gratis == 1 else f"{meses_gratis} meses gratis"
        explicacion.append(f"Tiene {meses}: {rango(activacion, gratis_hasta, activacion)} no paga nada.")
    else:
        explicacion.append(f"No tiene mes gratis: el servicio se cobra desde hoy, {f(activacion)}.")
    if prorrateo:
        dias = "1 día" if prorrateo.dias == 1 else f"{prorrateo.dias} días"
        explicacion.append(
            f"{rango(prorrateo.desde, prorrateo.hasta, activacion).capitalize()} se cobran solo esos {dias}: "
            f"{pesos(prorrateo.total)} (prorrateo), para que su pago quede siempre el día {primer_pago.day}."
        )
    cubre = f"cubre {rango(proxima_mensualidad, fin_primer_mes, activacion)}"
    if prorrateo:
        explicacion.append(
            f"El {f(primer_pago)} paga {pesos(total_primer_pago)} en un solo recibo: el prorrateo "
            f"({pesos(prorrateo.total)}) más su primera mensualidad ({pesos(mensualidad)}), que {cubre}."
        )
    else:
        explicacion.append(f"El {f(primer_pago)} paga su primera mensualidad de {pesos(mensualidad)}, que {cubre}.")
    explicacion.append(
        f"Después paga {pesos(mensualidad)} cada día {primer_pago.day}"
        + (", al terminar cada mes de servicio." if postpago else ", por adelantado el mes que empieza.")
    )
    if dias_tolerancia:
        explicacion.append(
            f"Si no paga a tiempo tiene {dias_tolerancia} días de tolerancia; "
            f"después del {f(corte)} el servicio se suspende."
        )

    return {
        # Fechas para pintar el calendario en la pantalla del técnico.
        "activacion": activacion.isoformat(),
        "mensualidad_desde": proxima_mensualidad.isoformat(),
        "mensualidad_hasta": fin_primer_mes.isoformat(),
        "corte": corte.isoformat() if dias_tolerancia else None,
        "mensualidad": float(mensualidad),
        "meses_gratis": meses_gratis,
        "gratis_hasta": gratis_hasta.isoformat() if gratis_hasta else None,
        "prorrateo": (
            {
                "total": float(prorrateo.total),
                "desde": prorrateo.desde.isoformat(),
                "hasta": prorrateo.hasta.isoformat(),
                "dias": prorrateo.dias,
            }
            if prorrateo else None
        ),
        "primer_pago": {"fecha": primer_pago.isoformat(), "total": float(total_primer_pago)},
        "explicacion": explicacion,
    }
