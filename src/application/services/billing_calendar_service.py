import calendar
from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class ServiceBillingDates:
    fecha_instalacion: date
    fecha_activacion: date
    fecha_inicio_servicio: date
    fecha_fin_periodo_gratis: date | None
    fecha_inicio_cobro: date
    proxima_facturacion: date


class BillingCalendarService:
    @staticmethod
    def add_months(fecha: date, meses: int) -> date:
        """
        Suma meses naturales conservando el día cuando sea posible.

        Ejemplos:
        15/07 + 1 mes = 15/08
        31/01 + 1 mes = 28/02 o 29/02
        """
        if meses < 0:
            raise ValueError("Los meses no pueden ser negativos.")

        total_meses = fecha.year * 12 + fecha.month - 1 + meses
        anio = total_meses // 12
        mes = total_meses % 12 + 1

        ultimo_dia = calendar.monthrange(anio, mes)[1]
        dia = min(fecha.day, ultimo_dia)

        return date(anio, mes, dia)

    @staticmethod
    def primer_dia_mes_siguiente(fecha: date) -> date:
        if fecha.month == 12:
            return date(fecha.year + 1, 1, 1)

        return date(fecha.year, fecha.month + 1, 1)

    @classmethod
    def calcular_fechas_servicio(
        cls,
        fecha_instalacion: date,
        fecha_activacion: date | None = None,
        meses_gratis: int = 1,
        ciclo_facturacion: str = "calendario",
    ) -> ServiceBillingDates:
        if meses_gratis < 0:
            raise ValueError("Los meses gratis no pueden ser negativos.")

        if ciclo_facturacion not in {"calendario", "aniversario"}:
            raise ValueError("El ciclo de facturación no es válido.")

        activacion = fecha_activacion or fecha_instalacion

        if activacion < fecha_instalacion:
            raise ValueError(
                "La fecha de activación no puede ser anterior "
                "a la fecha de instalación."
            )

        if meses_gratis > 0:
            fecha_fin_gratis = cls.add_months(
                activacion,
                meses_gratis,
            )

            fecha_inicio_cobro = fecha_fin_gratis + timedelta(days=1)
        else:
            fecha_fin_gratis = None
            fecha_inicio_cobro = activacion

        if ciclo_facturacion == "calendario":
            proxima_facturacion = fecha_inicio_cobro
        else:
            proxima_facturacion = fecha_inicio_cobro

        return ServiceBillingDates(
            fecha_instalacion=fecha_instalacion,
            fecha_activacion=activacion,
            fecha_inicio_servicio=activacion,
            fecha_fin_periodo_gratis=fecha_fin_gratis,
            fecha_inicio_cobro=fecha_inicio_cobro,
            proxima_facturacion=proxima_facturacion,
        )