import re

# ==========================================
# ARCHIVO MAESTRO DE INTELIGENCIA FdezNet
# Catálogo de OIDs por Modelo de OLT (Exclusivo VSOL)
# ==========================================

MAPA_OIDS = {
    # ==========================================
    # 🔵 OLTs GPON (VSOL)
    # ==========================================
    # Modelo de 1 Puerto PON
    'V1600GS': {
        'TIPO': 'GPON',
        'ID_NAME': 'SERIAL',
        'RAMA_IDS':         '.1.3.6.1.4.1.37950.1.1.6.3.5.1.7', 
        'RAMA_POTENCIA':    '.1.3.6.1.4.1.37950.1.1.6.1.1.3.1.7', 
        'RAMA_TEMP':        '.1.3.6.1.4.1.37950.1.1.6.1.1.3.1.3',
    },
    # Modelo de 4 Puertos PON (Nombre compatible con Frontend)
    'V1600G0-B': { 
        'TIPO': 'GPON',
        'ID_NAME': 'SERIAL',
        'RAMA_IDS':         '.1.3.6.1.4.1.37950.1.1.6.1.1.2.1.5', # OID validado en pruebas
        'RAMA_POTENCIA':    '.1.3.6.1.4.1.37950.1.1.6.1.1.3.1.7', 
        'RAMA_TEMP':        '.1.3.6.1.4.1.37950.1.1.6.1.1.3.1.3',
    },
    
    # ==========================================
    # 🟢 OLTs EPON (VSOL)
    # ==========================================
    # Modelo de 2 Puertos PON
    'V1601E02-DP': {
        'TIPO': 'EPON',
        'ID_NAME': 'MAC',
        'RAMA_IDS':         '.1.3.6.1.4.1.37950.1.1.5.12.1.9.1.5', 
        # Tabla de ONU registradas: columna 2 = PON, 3 = número de ONU, 6 = MAC.
        # La potencia viene indexada por PON.ONU; la tabla RAMA_IDS (lista de
        # autorización) tiene otra numeración y no sirve para cruzarla.
        'RAMA_REGISTRO':    '.1.3.6.1.4.1.37950.1.1.5.12.1.12.1',
        'RAMA_POTENCIA':    '.1.3.6.1.4.1.37950.1.1.5.12.2.1.8.1.7', 
        'RAMA_TEMP':        '.1.3.6.1.4.1.37950.1.1.5.12.2.1.8.1.3',
    }
}

def procesar_potencia(texto: str, tipo_tecnologia: str) -> str:
    """Limpia y extrae el valor numérico de la potencia óptica según la tecnología."""
    if not texto or "N/A" in texto:
        return "0.00"
    
    if tipo_tecnologia == 'EPON':
        # V-SOL EPON: Extrae '-17.28' de '0.02 mW (-17.28 dBm)'
        match = re.search(r'\((.*?) dBm\)', texto)
        return match.group(1) if match else texto
            
    # GPON (V-SOL): Ya viene limpio desde el SNMP (ej. "-22.01")
    return texto.strip().replace('"', '')


def _filas_walk(texto: str, rama: str):
    """Líneas de snmpwalk -On → (índice tras la rama, valor sin comillas)."""
    rama = rama.strip().lstrip(".")
    for linea in (texto or "").splitlines():
        if "=" not in linea:
            continue
        oid, valor = (parte.strip() for parte in linea.split("=", 1))
        oid = oid.lstrip(".")
        if not oid.startswith(rama + "."):
            continue
        if ":" in valor:
            valor = valor.split(":", 1)[1]
        yield oid[len(rama) + 1:], valor.strip().strip('"').strip()


def cruzar_epon(registro: str, potencias: str, conf: dict) -> list[dict]:
    """Une la tabla de registro (PON, ONU, MAC) con la potencia (índice PON.ONU)."""
    columnas: dict[str, dict[str, str]] = {}
    for indice, valor in _filas_walk(registro, conf["RAMA_REGISTRO"]):
        columna, _, fila = indice.partition(".")
        columnas.setdefault(fila, {})[columna] = valor
    mac_por_indice = {
        f"{c['2']}.{c['3']}": (c["6"], c["2"])
        for c in columnas.values()
        if c.get("2") and c.get("3") and c.get("6")
    }
    onus = []
    for indice, valor in _filas_walk(potencias, conf["RAMA_POTENCIA"]):
        encontrado = mac_por_indice.get(indice)
        if not encontrado:
            continue
        mac, pon = encontrado
        onus.append({"identificador": mac, "potencia": procesar_potencia(valor, "EPON"), "pon": pon})
    return onus
