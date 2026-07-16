import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional


@dataclass(frozen=True)
class PeriodoFacturacionCalculado:
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
    fecha_vencimiento: date
    fecha_limite_corte: date
    fecha_generacion: date
    siguiente_facturacion: date
    mes_correspondiente: str


class IspBillingCycleService:
    """
    Motor de ciclos ISP/WISP adaptable por plantilla.

    Regla:
    - La plantilla define el dia de pago/inicio del ciclo.
    - Prepago vence al inicio del periodo normal.
    - Postpago vence al siguiente inicio de ciclo.
    - El prorrateo inicial cierra un dia antes del proximo inicio de ciclo.
    - El corte se calcula como vencimiento + dias_tolerancia.
    """

    @staticmethod
    def _to_decimal(value) -> Decimal:
        if value is None:
            return Decimal('0.00')
        return Decimal(str(value))

    @staticmethod
    def money(value: Decimal) -> Decimal:
        return value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    @staticmethod
    def rate4(value: Decimal) -> Decimal:
        return value.quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)

    @staticmethod
    def ultimo_dia_mes(anio: int, mes: int) -> int:
        return calendar.monthrange(anio, mes)[1]

    @classmethod
    def fecha_segura(cls, anio: int, mes: int, dia: int) -> date:
        dia = max(1, int(dia or 1))
        return date(anio, mes, min(dia, cls.ultimo_dia_mes(anio, mes)))

    @staticmethod
    def sumar_meses(fecha: date, meses: int) -> date:
        total = fecha.year * 12 + fecha.month - 1 + meses
        anio = total // 12
        mes = total % 12 + 1
        dia = min(fecha.day, calendar.monthrange(anio, mes)[1])
        return date(anio, mes, dia)

    @classmethod
    def inicio_ciclo_en_o_antes(cls, fecha: date, dia_pago: int) -> date:
        candidato = cls.fecha_segura(fecha.year, fecha.month, dia_pago)
        if candidato <= fecha:
            return candidato
        mes_anterior = cls.sumar_meses(date(fecha.year, fecha.month, 1), -1)
        return cls.fecha_segura(mes_anterior.year, mes_anterior.month, dia_pago)

    @classmethod
    def siguiente_inicio_ciclo(cls, fecha: date, dia_pago: int) -> date:
        """Primer inicio de ciclo estrictamente posterior a fecha."""
        candidato = cls.fecha_segura(fecha.year, fecha.month, dia_pago)
        if candidato > fecha:
            return candidato
        mes_siguiente = cls.sumar_meses(date(fecha.year, fecha.month, 1), 1)
        return cls.fecha_segura(mes_siguiente.year, mes_siguiente.month, dia_pago)

    @classmethod
    def es_inicio_de_ciclo(cls, fecha: date, dia_pago: int) -> bool:
        return cls.inicio_ciclo_en_o_antes(fecha, dia_pago) == fecha

    @classmethod
    def calcular_periodo(
        cls,
        fecha_base: date,
        dia_pago: int,
        tipo_facturacion: str = 'prepago',
        precio_mensual=0,
        impuesto_porcentaje=0,
        dias_tolerancia: int = 0,
        dias_antes_emision: int = 0,
        politica_prorrateo: str = 'dias_reales_periodo',
        politica_cobro_prorrateo: str = 'siguiente_ciclo',
    ) -> PeriodoFacturacionCalculado:
        if not fecha_base:
            raise ValueError('fecha_base es obligatoria')

        dia_pago = int(dia_pago or 1)
        dias_tolerancia = int(dias_tolerancia or 0)
        dias_antes_emision = int(dias_antes_emision or 0)
        tipo_facturacion = (tipo_facturacion or 'prepago').lower()
        politica_cobro_prorrateo = (politica_cobro_prorrateo or 'siguiente_ciclo').lower()

        inicio_normal = cls.es_inicio_de_ciclo(fecha_base, dia_pago)
        siguiente_inicio = cls.siguiente_inicio_ciclo(fecha_base, dia_pago)

        periodo_desde = fecha_base
        periodo_hasta = siguiente_inicio - timedelta(days=1)
        dias_facturados = (periodo_hasta - periodo_desde).days + 1
        dias_periodo = (periodo_hasta - cls.inicio_ciclo_en_o_antes(periodo_hasta, dia_pago)).days + 1

        # Para ciclos normales, dias_periodo debe representar todo el ciclo completo.
        # Ej: ciclo 15 al 14 = dias entre el inicio y el siguiente inicio.
        if inicio_normal:
            dias_periodo = (siguiente_inicio - periodo_desde).days
        else:
            ciclo_completo_inicio = cls.inicio_ciclo_en_o_antes(periodo_hasta, dia_pago)
            ciclo_completo_fin = cls.siguiente_inicio_ciclo(ciclo_completo_inicio, dia_pago)
            dias_periodo = (ciclo_completo_fin - ciclo_completo_inicio).days

        es_prorrateada = not inicio_normal

        precio = cls.money(cls._to_decimal(precio_mensual))
        impuesto_pct = cls._to_decimal(impuesto_porcentaje)

        if politica_prorrateo == 'base_30_dias':
            divisor = Decimal('30')
        else:
            divisor = Decimal(str(dias_periodo or 1))

        if es_prorrateada:
            precio_diario = cls.rate4(precio / divisor)
            subtotal = cls.money(precio_diario * Decimal(str(dias_facturados)))
        else:
            precio_diario = cls.rate4(precio / divisor)
            subtotal = precio

        impuesto = cls.money(subtotal * impuesto_pct / Decimal('100'))
        total = cls.money(subtotal + impuesto)

        if tipo_facturacion == 'postpago':
            fecha_vencimiento = siguiente_inicio
        elif es_prorrateada and politica_cobro_prorrateo == 'inicio_cobro':
            fecha_vencimiento = periodo_desde
        else:
            # Prepago normal y prorrateo cobrado al llegar al siguiente ciclo.
            fecha_vencimiento = siguiente_inicio if es_prorrateada else periodo_desde

        fecha_limite_corte = fecha_vencimiento + timedelta(days=dias_tolerancia)
        fecha_generacion = fecha_vencimiento - timedelta(days=dias_antes_emision)

        if es_prorrateada:
            mes_correspondiente = (
                f'Prorrateo {periodo_desde.strftime("%d/%m/%Y")} - '
                f'{periodo_hasta.strftime("%d/%m/%Y")}'
            )
        else:
            mes_correspondiente = (
                f'Ciclo {periodo_desde.strftime("%d/%m/%Y")} - '
                f'{periodo_hasta.strftime("%d/%m/%Y")}'
            )

        return PeriodoFacturacionCalculado(
            periodo_desde=periodo_desde,
            periodo_hasta=periodo_hasta,
            dias_facturados=dias_facturados,
            dias_periodo=dias_periodo,
            precio_mensual=precio,
            precio_diario=precio_diario,
            subtotal=subtotal,
            impuesto=impuesto,
            total=total,
            es_prorrateada=es_prorrateada,
            fecha_vencimiento=fecha_vencimiento,
            fecha_limite_corte=fecha_limite_corte,
            fecha_generacion=fecha_generacion,
            siguiente_facturacion=siguiente_inicio,
            mes_correspondiente=mes_correspondiente,
        )
