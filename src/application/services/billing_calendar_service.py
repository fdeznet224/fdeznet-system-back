import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any


CENTAVOS = Decimal("0.01")
CUATRO_DECIMALES = Decimal("0.0001")


@dataclass(frozen=True)
class ServiceBillingDates:
    fecha_instalacion: date
    fecha_activacion: date
    fecha_inicio_servicio: date
    fecha_fin_periodo_gratis: date | None
    fecha_inicio_cobro: date
    proxima_facturacion: date


@dataclass(frozen=True)
class BillingPeriodCalculation:
    periodo_desde: date
    periodo_hasta: date
    dias_facturados: int
    dias_periodo: int
    precio_mensual: Decimal
    precio_diario: Decimal
    subtotal: Decimal
    impuesto: Decimal
    total: Decimal
    es_prorrateada: bool
    siguiente_facturacion: date


class BillingCalendarService:
    @staticmethod
    def to_decimal(value: Any) -> Decimal:
        if value is None:
            return Decimal("0")
        return Decimal(str(value))

    @staticmethod
    def money(value: Decimal) -> Decimal:
        return value.quantize(CENTAVOS, rounding=ROUND_HALF_UP)

    @staticmethod
    def daily_money(value: Decimal) -> Decimal:
        return value.quantize(CUATRO_DECIMALES, rounding=ROUND_HALF_UP)

    @staticmethod
    def add_months(fecha: date, meses: int) -> date:
        if meses < 0:
            raise ValueError("Los meses no pueden ser negativos.")
        total_meses = fecha.year * 12 + fecha.month - 1 + meses
        anio = total_meses // 12
        mes = total_meses % 12 + 1
        ultimo_dia = calendar.monthrange(anio, mes)[1]
        dia = min(fecha.day, ultimo_dia)
        return date(anio, mes, dia)

    @staticmethod
    def ultimo_dia_mes(fecha: date) -> date:
        ultimo_dia = calendar.monthrange(fecha.year, fecha.month)[1]
        return date(fecha.year, fecha.month, ultimo_dia)

    @staticmethod
    def primer_dia_mes_siguiente(fecha: date) -> date:
        if fecha.month == 12:
            return date(fecha.year + 1, 1, 1)
        return date(fecha.year, fecha.month + 1, 1)

    @staticmethod
    def _validar_dia_ciclo(dia_ciclo: int | None) -> int:
        if dia_ciclo is None:
            return 1
        dia = int(dia_ciclo)
        if dia < 1 or dia > 31:
            raise ValueError("El día de ciclo debe estar entre 1 y 31.")
        return dia

    @classmethod
    def fecha_inicio_ciclo_mes(cls, anio: int, mes: int, dia_ciclo: int | None) -> date:
        dia = cls._validar_dia_ciclo(dia_ciclo)
        ultimo_dia = calendar.monthrange(anio, mes)[1]
        return date(anio, mes, min(dia, ultimo_dia))

    @classmethod
    def mes_anterior(cls, fecha: date) -> date:
        if fecha.month == 1:
            return date(fecha.year - 1, 12, 1)
        return date(fecha.year, fecha.month - 1, 1)

    @classmethod
    def inicio_ciclo_actual(cls, fecha: date, dia_ciclo: int | None) -> date:
        inicio_mes_actual = cls.fecha_inicio_ciclo_mes(fecha.year, fecha.month, dia_ciclo)
        if fecha >= inicio_mes_actual:
            return inicio_mes_actual
        mes_anterior = cls.mes_anterior(fecha)
        return cls.fecha_inicio_ciclo_mes(mes_anterior.year, mes_anterior.month, dia_ciclo)

    @classmethod
    def siguiente_inicio_ciclo(cls, fecha: date, dia_ciclo: int | None) -> date:
        inicio_mes_actual = cls.fecha_inicio_ciclo_mes(fecha.year, fecha.month, dia_ciclo)
        if fecha < inicio_mes_actual:
            return inicio_mes_actual
        siguiente_mes = cls.primer_dia_mes_siguiente(fecha)
        return cls.fecha_inicio_ciclo_mes(siguiente_mes.year, siguiente_mes.month, dia_ciclo)

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
            raise ValueError("La fecha de activación no puede ser anterior a la fecha de instalación.")
        if meses_gratis > 0:
            fecha_fin_gratis = cls.add_months(activacion, meses_gratis)
            fecha_inicio_cobro = fecha_fin_gratis + timedelta(days=1)
        else:
            fecha_fin_gratis = None
            fecha_inicio_cobro = activacion
        return ServiceBillingDates(
            fecha_instalacion=fecha_instalacion,
            fecha_activacion=activacion,
            fecha_inicio_servicio=activacion,
            fecha_fin_periodo_gratis=fecha_fin_gratis,
            fecha_inicio_cobro=fecha_inicio_cobro,
            proxima_facturacion=fecha_inicio_cobro,
        )

    @classmethod
    def calcular_periodo_por_dia_ciclo(
        cls,
        periodo_desde: date,
        dia_ciclo: int | None,
        precio_mensual: float | Decimal,
        impuesto_porcentaje: float | Decimal = 0,
    ) -> BillingPeriodCalculation:
        inicio_ciclo = cls.inicio_ciclo_actual(periodo_desde, dia_ciclo)
        siguiente_ciclo = cls.siguiente_inicio_ciclo(periodo_desde, dia_ciclo)
        fin_ciclo = siguiente_ciclo - timedelta(days=1)
        if fin_ciclo < periodo_desde:
            raise ValueError("No fue posible calcular un periodo válido para la plantilla.")
        precio = cls.to_decimal(precio_mensual)
        impuesto_pct = cls.to_decimal(impuesto_porcentaje)
        dias_periodo = (fin_ciclo - inicio_ciclo).days + 1
        dias_facturados = (fin_ciclo - periodo_desde).days + 1
        precio_diario = cls.daily_money(precio / Decimal(dias_periodo))
        subtotal = cls.money(precio_diario * Decimal(dias_facturados))
        impuesto = cls.money(subtotal * impuesto_pct / Decimal("100"))
        total = cls.money(subtotal + impuesto)
        return BillingPeriodCalculation(
            periodo_desde=periodo_desde,
            periodo_hasta=fin_ciclo,
            dias_facturados=dias_facturados,
            dias_periodo=dias_periodo,
            precio_mensual=cls.money(precio),
            precio_diario=precio_diario,
            subtotal=subtotal,
            impuesto=impuesto,
            total=total,
            es_prorrateada=periodo_desde != inicio_ciclo,
            siguiente_facturacion=siguiente_ciclo,
        )

    @classmethod
    def calcular_fecha_vencimiento(cls, periodo: BillingPeriodCalculation, tipo_facturacion: str) -> date:
        if tipo_facturacion == "postpago":
            return periodo.siguiente_facturacion
        return periodo.periodo_desde

    @classmethod
    def calcular_fecha_generacion(
        cls,
        periodo: BillingPeriodCalculation,
        tipo_facturacion: str,
        dias_antes_emision: int | None = 0,
    ) -> date:
        vencimiento = cls.calcular_fecha_vencimiento(periodo, tipo_facturacion)
        fecha_generacion = vencimiento - timedelta(days=dias_antes_emision or 0)
        if tipo_facturacion == "postpago":
            primer_dia_despues_periodo = periodo.periodo_hasta + timedelta(days=1)
            if fecha_generacion < primer_dia_despues_periodo:
                return primer_dia_despues_periodo
        return fecha_generacion

    @classmethod
    def calcular_periodo_calendario(
        cls,
        periodo_desde: date,
        periodo_hasta: date,
        precio_mensual: float | Decimal,
        impuesto_porcentaje: float | Decimal = 0,
    ) -> BillingPeriodCalculation:
        if periodo_hasta < periodo_desde:
            raise ValueError("El periodo hasta no puede ser anterior al periodo desde.")
        if periodo_desde.year != periodo_hasta.year or periodo_desde.month != periodo_hasta.month:
            raise ValueError("El periodo calendario debe estar dentro del mismo mes.")
        precio = cls.to_decimal(precio_mensual)
        impuesto_pct = cls.to_decimal(impuesto_porcentaje)
        dias_periodo = calendar.monthrange(periodo_desde.year, periodo_desde.month)[1]
        dias_facturados = (periodo_hasta - periodo_desde).days + 1
        precio_diario = cls.daily_money(precio / Decimal(dias_periodo))
        subtotal = cls.money(precio_diario * Decimal(dias_facturados))
        impuesto = cls.money(subtotal * impuesto_pct / Decimal("100"))
        total = cls.money(subtotal + impuesto)
        return BillingPeriodCalculation(
            periodo_desde=periodo_desde,
            periodo_hasta=periodo_hasta,
            dias_facturados=dias_facturados,
            dias_periodo=dias_periodo,
            precio_mensual=cls.money(precio),
            precio_diario=precio_diario,
            subtotal=subtotal,
            impuesto=impuesto,
            total=total,
            es_prorrateada=dias_facturados != dias_periodo,
            siguiente_facturacion=cls.primer_dia_mes_siguiente(periodo_desde),
        )

    @classmethod
    def calcular_prorrateo_calendario(
        cls,
        fecha_inicio_cobro: date,
        precio_mensual: float | Decimal,
        impuesto_porcentaje: float | Decimal = 0,
    ) -> BillingPeriodCalculation:
        return cls.calcular_periodo_por_dia_ciclo(
            periodo_desde=fecha_inicio_cobro,
            dia_ciclo=1,
            precio_mensual=precio_mensual,
            impuesto_porcentaje=impuesto_porcentaje,
        )

    @classmethod
    def calcular_mensualidad_calendario(
        cls,
        fecha_periodo: date,
        precio_mensual: float | Decimal,
        impuesto_porcentaje: float | Decimal = 0,
    ) -> BillingPeriodCalculation:
        return cls.calcular_periodo_por_dia_ciclo(
            periodo_desde=fecha_periodo,
            dia_ciclo=1,
            precio_mensual=precio_mensual,
            impuesto_porcentaje=impuesto_porcentaje,
        )
