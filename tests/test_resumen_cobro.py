from datetime import date
from decimal import Decimal

from src.application.services.resumen_cobro import Prorrateo, resumen_cobro


def test_mes_gratis_con_prorrateo_como_guadalupe():
    resumen = resumen_cobro(
        activacion=date(2026, 10, 5),
        meses_gratis=1,
        gratis_hasta=date(2026, 11, 4),
        proxima_mensualidad=date(2026, 11, 15),
        tipo_facturacion="prepago",
        precio_mensual=Decimal("350"),
        impuesto_porcentaje=Decimal("0"),
        dias_tolerancia=10,
        prorrateo=Prorrateo(Decimal("112.90"), date(2026, 11, 5), date(2026, 11, 14), 10),
    )
    assert resumen["primer_pago"] == {"fecha": "2026-11-15", "total": 462.90}
    assert (resumen["activacion"], resumen["mensualidad_desde"], resumen["mensualidad_hasta"], resumen["corte"]) == (
        "2026-10-05", "2026-11-15", "2026-12-14", "2026-11-25")
    assert resumen["explicacion"] == [
        "Tiene 1 mes gratis: del 5 de octubre al 4 de noviembre no paga nada.",
        "Del 5 al 14 de noviembre se cobran solo esos 10 días: $112.90 (prorrateo), "
        "para que su pago quede siempre el día 15.",
        "El 15 de noviembre paga $462.90 en un solo recibo: el prorrateo ($112.90) más su primera "
        "mensualidad ($350.00), que cubre del 15 de noviembre al 14 de diciembre.",
        "Después paga $350.00 cada día 15, por adelantado el mes que empieza.",
        "Si no paga a tiempo tiene 10 días de tolerancia; después del 25 de noviembre el servicio se suspende.",
    ]


def test_cambio_de_compania_sin_prorrateo_el_dia_de_pago():
    resumen = resumen_cobro(
        activacion=date(2026, 10, 15),
        meses_gratis=0,
        gratis_hasta=None,
        proxima_mensualidad=date(2026, 10, 15),
        tipo_facturacion="prepago",
        precio_mensual=Decimal("300"),
        impuesto_porcentaje=Decimal("16"),
        dias_tolerancia=0,
        prorrateo=None,
    )
    assert resumen["mensualidad"] == 348.0
    assert resumen["prorrateo"] is None
    assert resumen["explicacion"] == [
        "No tiene mes gratis: el servicio se cobra desde hoy, 15 de octubre.",
        "El 15 de octubre paga su primera mensualidad de $348.00, que cubre del 15 de octubre al 14 de noviembre.",
        "Después paga $348.00 cada día 15, por adelantado el mes que empieza.",
    ]


def test_postpago_paga_al_terminar_el_mes_y_cambia_de_anio():
    resumen = resumen_cobro(
        activacion=date(2026, 12, 20),
        meses_gratis=0,
        gratis_hasta=None,
        proxima_mensualidad=date(2027, 1, 1),
        tipo_facturacion="postpago",
        precio_mensual=Decimal("400"),
        impuesto_porcentaje=Decimal("0"),
        dias_tolerancia=3,
        prorrateo=Prorrateo(Decimal("154.84"), date(2026, 12, 20), date(2026, 12, 31), 12),
    )
    assert resumen["primer_pago"]["fecha"] == "2027-02-01"
    assert "El 1 de febrero de 2027 paga $554.84 en un solo recibo" in resumen["explicacion"][2]
    assert resumen["explicacion"][3] == "Después paga $400.00 cada día 1, al terminar cada mes de servicio."
