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