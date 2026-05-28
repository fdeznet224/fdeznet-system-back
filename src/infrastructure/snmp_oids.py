import re

# ==========================================
# ARCHIVO MAESTRO DE INTELIGENCIA FdezNet
# Catálogo de OIDs por Modelo de OLT
# ==========================================

MAPA_OIDS = {
    'V1600GS': {
        'TIPO': 'GPON',
        'ID_NAME': 'SERIAL',
        'RAMA_IDS':         '.1.3.6.1.4.1.37950.1.1.6.3.5.1.7', 
        'RAMA_POTENCIA':    '.1.3.6.1.4.1.37950.1.1.6.1.1.3.1.7', 
        'RAMA_TEMP':        '.1.3.6.1.4.1.37950.1.1.6.1.1.3.1.3',
    },
    'V1601E02-DP': {
        'TIPO': 'EPON',
        'ID_NAME': 'MAC',
        'RAMA_IDS':         '.1.3.6.1.4.1.37950.1.1.5.12.1.9.1.5', 
        'RAMA_POTENCIA':    '.1.3.6.1.4.1.37950.1.1.5.12.2.1.8.1.7', 
        'RAMA_TEMP':        '.1.3.6.1.4.1.37950.1.1.5.12.2.1.8.1.3',
    },
    'HIOSO-HA7104': {  
        'TIPO': 'EPON_HIOSO',
        'ID_NAME': 'MAC',
        'RAMA_IDS':         '.1.3.6.1.4.1.25355.3.2.6.3.2.1.11.1', 
        'RAMA_POTENCIA':    '.1.3.6.1.4.1.25355.3.2.6.14.2.1.8.1', 
        'RAMA_TEMP':        '.1.3.6.1.4.1.25355.3.2.6.14.2.1.7.1',
    }
}

def procesar_potencia(texto: str, tipo_tecnologia: str) -> str:
    if not texto or "N/A" in texto:
        return "0.00"
    
    if tipo_tecnologia == 'EPON':
        # V-SOL EPON: Extrae '-17.28' de '0.02 mW (-17.28 dBm)'
        match = re.search(r'\((.*?) dBm\)', texto)
        return match.group(1) if match else texto
        
    elif tipo_tecnologia == 'EPON_HIOSO':
        # HIOSO: Ya viene limpio como "-23.28", solo quitamos comillas si trae
        return texto.strip().replace('"', '')
            
    # GPON (V-SOL): Ya viene limpio
    return texto.strip().replace('"', '')

def formatear_mac_hioso(mac_string: str) -> str:
    """Convierte '481258a07d34' a '48:12:58:A0:7D:34' para cruzar con BD"""
    mac_limpia = mac_string.strip().replace('"', '').upper()
    if len(mac_limpia) == 12:
        return ':'.join(mac_limpia[i:i+2] for i in range(0, 12, 2))
    return mac_string