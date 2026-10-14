"""OLT V-SOL EPON por SNMP: la potencia se cruza con la MAC por PON y ONU.

La lista de autorización (RAMA_IDS) tiene su propia numeración: usarla con el
número de ONU daba a cada cliente la señal de otra ONU (Paraíso, oct 2026).
"""

import asyncio

from src.application.services.snmp_service import SNMPMonitorService
from src.infrastructure.snmp_oids import MAPA_OIDS, cruzar_epon

CONF = MAPA_OIDS["V1601E02-DP"]
REG = ".1.3.6.1.4.1.37950.1.1.5.12.1.12.1"
POT = ".1.3.6.1.4.1.37950.1.1.5.12.2.1.8.1.7"


def fila(numero, pon, onu, mac):
    return "\n".join([
        f"{REG}.1.{numero} = INTEGER: {numero}",
        f"{REG}.2.{numero} = INTEGER: {pon}",
        f"{REG}.3.{numero} = INTEGER: {onu}",
        f"{REG}.4.{numero} = INTEGER: 1",
        f'{REG}.6.{numero} = STRING: "{mac}"',
        f'{REG}.14.{numero} = STRING: "4GE+1POTS+2WiFi"',
    ])


REGISTRO = "\n".join([
    fila(1, 1, 2, "aa:00:00:00:00:12"),
    fila(5, 2, 1, "aa:00:00:00:00:21"),
    fila(6, 1, 1, "aa:00:00:00:00:11"),
    fila(7, 1, 24, "aa:00:00:00:01:24"),
    fila(8, 2, 4, "aa:00:00:00:00:24"),
])
POTENCIAS = "\n".join([
    f'{POT}.1.1 = STRING: "0.02 mW (-17.54 dBm)"',
    f'{POT}.1.2 = STRING: "0.00 mW (-23.19 dBm)"',
    f'{POT}.1.24 = STRING: "0.03 mW (-14.83 dBm)"',
    f'{POT}.2.1 = STRING: "0.01 mW (-18.39 dBm)"',
    f'{POT}.2.4 = ""',
    f'{POT}.2.9 = STRING: "0.02 mW (-16.58 dBm)"',
])


def test_cruza_por_pon_y_numero_de_onu():
    onus = {o["identificador"]: o for o in cruzar_epon(REGISTRO, POTENCIAS, CONF)}

    assert onus["aa:00:00:00:00:11"] == {"identificador": "aa:00:00:00:00:11", "potencia": "-17.54", "pon": "1"}
    assert onus["aa:00:00:00:00:12"]["potencia"] == "-23.19"
    assert onus["aa:00:00:00:01:24"]["potencia"] == "-14.83"
    # Misma ONU 1 pero en el PON 2: no se confunde con la del PON 1.
    assert onus["aa:00:00:00:00:21"] == {"identificador": "aa:00:00:00:00:21", "potencia": "-18.39", "pon": "2"}
    # Apagada: aparece, sin potencia.
    assert onus["aa:00:00:00:00:24"]["potencia"] == "0.00"
    # Potencia de una ONU que no está en el registro: se ignora.
    assert len(onus) == 5


def test_escaneo_epon_con_estado_y_pon():
    servicio = SNMPMonitorService(db=None)

    async def comando(*args):
        if args[-1].startswith(CONF["RAMA_REGISTRO"] + "."):
            columna = args[-1].rsplit(".", 1)[1]
            return "\n".join(l for l in REGISTRO.splitlines() if l.startswith(f"{REG}.{columna}."))
        return POTENCIAS

    servicio._ejecutar_comando = comando
    onus = asyncio.run(servicio._escanear_olt_fisica_directo("10.0.0.1", "public", "V1601E02-DP"))
    por_mac = {o["identificador"]: o for o in onus}

    assert por_mac["AA:00:00:00:00:21"] == {
        "identificador": "AA:00:00:00:00:21", "pon": "2", "potencia": "-18.39", "status": "online",
    }
    assert por_mac["AA:00:00:00:00:24"]["status"] == "offline"
