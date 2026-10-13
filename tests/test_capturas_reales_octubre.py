"""Texto del OCR de capturas reales de octubre (nombres cambiados)."""

import asyncio
from datetime import datetime

import pytest

from src.application.services.comprobante_service import ComprobanteService
from src.application.services.ocr_service import OCRService


def _leer(texto):
    return OCRService.extraer_datos(texto.replace(" | ", " "))


BANCOPPEL_SIN_PUNTO = (
    "Transferencia exitosa | $30000 | Monto transferido | 08/10/2026 | 08:36:59 a.m. | Fecha y hora de la "
    "transacción | cuenta destino | AZTECA | 5265 | juan perez | Cuenta origen | CUENTA EFECTIVA DIGITAL *8583 | "
    "Bancoppel | Datos de tu transferencia | Concepto | internet joel | Referencia | 0810260 | Comisión | $0.00 | "
    "Tu clave de rastreo es: | 50115838TRANSBPI89256280"
)
BANCOPPEL_DETALLE = (
    "6:34 | Yé | 4GI/ | 80 | Detalle del movimiento | ~830000 | Monto | pago | concepto | Banco origen | "
    "BANCOPPEL | JUAN PEREZ | Cliente origen | cuenta origen | 6536 | 11/10/2026 | Fecha y hora de operación | "
    "6:18 a.m. | Banco destino | AZTECA | cuenta destino | 5265 | Referencia | 1110260 | Folio de operación | "
    "0529087380208575 | Desde | Bancoppel | Tu clave de rastreo es: | 50116536TRANSBPI36976460"
)
BBVA = (
    "BBVA | COMPROBANTE DE LA OPERACION | Tipo de operación | Transferencia a otros bancos | Folio de la "
    "operación | 0798256198 | Fecha | 12 octubre 2026 | Hora | 12:50 h | Concepto | AsC1 | Clave de rastreo | "
    "MBANO10O2610120055871643 | Importe transferido | $ 354.00 | Cuenta de origen | '7640"
)
MERCADO_PAGO = (
    "mercado | WALLET | pago | Comprobante de transferencia | I1/octubre/2026 a las 10.45. | $ 300 MXN | "
    "Origen y destino | Juan Perez | Mercado Pago Wallet | CLABE: ****1192 | AZTECA | Número de tarjeta: "
    "****5265 | Comprobante | #183642536052"
)
AZTECA_TRASPASO = (
    "8300.00 | Traspaso De Cuenta | 07 Oct 2026 | Realizado con | Nomina Azteca Tradicional ***7358 | Para | "
    "Para Juan Perez ****6342 | de operación | Traspaso 94641303036342 | Hora | 17.07.10 | Folio | 3214"
)
AZTECA_GUARDADITO = (
    "Enviaste a una cuenta | 8330.00 | 11/0ct/2026 | 08.51:18 (CST) | Cuenta origen | Guardadito ***9678 | "
    "Banco Azteca ***265 | Concepto | Armando | Folio: MX216335339"
)
ETIQUETA_MODEM = (
    "WPS | WLAN | USB | LAN4 | iNO MANIPULAR ESTE EQUIPOI | Reconfiguración tiene costo adicional | 9 | "
    "FDEZ NET | Red inalámbrica: | SIEMPRE CONECTADOS | FdezNet- | F766 | eTienes dudas 0 fallas? | "
    "Contraseña: | 961 363 2496"
)
PANTALLA_WIFI = "1:58 | Wi-Fi | Activado | Red actual | FdezNet-14A9 | Conectada | Redes disponibles | Zona FdezNet"


@pytest.mark.parametrize("texto, folio, monto", [
    # BanCoppel pierde el punto ("$30000") o lee el "$" como "~8".
    (BANCOPPEL_SIN_PUNTO, "50115838TRANSBPI89256280", 300.0),
    (BANCOPPEL_DETALLE, "50116536TRANSBPI36976460", 300.0),
    # Banco Azteca: el "$" sale como 8.
    (AZTECA_TRASPASO, "94641303036342", 300.0),
    (AZTECA_GUARDADITO, "MX216335339", 330.0),
])
def test_monto_que_el_ocr_lee_sin_punto_o_con_el_signo_como_8(texto, folio, monto):
    datos = _leer(texto)
    assert datos["exito"] and datos["folio"] == folio and datos["monto"] == monto


def test_una_referencia_suelta_no_se_toma_como_monto():
    assert _leer("Transferencia | Referencia | 1110260 | Folio de operación | 0529087380208575")["monto"] == 0


def test_bbva_fecha_sin_de_entre_dia_mes_y_anio():
    datos = _leer(BBVA)
    assert datos["fecha_pago"] == datetime(2026, 10, 12, 12, 50)
    assert datos["monto"] == 354.0


def test_mercado_pago_folio_con_numeral_y_no_la_fecha():
    datos = _leer(MERCADO_PAGO)
    assert datos["folio"] == "183642536052"
    assert datos["fecha_pago"] == datetime(2026, 10, 11, 10, 45)


def test_la_etiqueta_del_modem_y_la_pantalla_wifi_traen_el_contrato():
    assert _leer(ETIQUETA_MODEM)["contratos_en_red_wifi"] == ["F766"]
    assert _leer(PANTALLA_WIFI)["contratos_en_red_wifi"] == ["14A9"]
    assert not _leer(ETIQUETA_MODEM)["parece_comprobante"]


class _DB:
    def __init__(self, contratos):
        self.contratos = contratos

    async def execute(self, _consulta):
        contratos = self.contratos

        class _R:
            def scalars(self):
                return self

            def all(self):
                return contratos

        return _R()


def _leer_foto(texto, contratos):
    class _OCR:
        async def procesar_ticket(self, _url):
            return _leer(texto)

    return asyncio.run(ComprobanteService(_DB(contratos), ocr=_OCR()).leer("whatsapp-media://m.jpg", "1@lid", None, 9))


def test_la_foto_del_modem_da_el_contrato_para_identificar_al_cliente():
    resultado = _leer_foto(ETIQUETA_MODEM, ["F766"])
    assert resultado["estado"] == "contrato_en_foto" and resultado["contrato"] == "F766"


def test_si_el_codigo_no_es_de_un_cliente_no_es_comprobante():
    assert _leer_foto(ETIQUETA_MODEM, [])["estado"] == "no_es_comprobante"


def test_la_s_del_ocr_en_el_contrato_es_un_5():
    from src.application.services.comprobante_service import normalizar_contrato

    assert normalizar_contrato(_leer(BBVA)["cedula_detectada"]) == "A5C1"
