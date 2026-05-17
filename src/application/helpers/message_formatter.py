import re
from typing import Dict, Any

def formatear_mensaje(plantilla: str, datos: Dict[str, Any]) -> str:
    """
    Toma una cadena con etiquetas tipo {variable} y las reemplaza 
    con los valores del diccionario de datos de forma segura.
    """
    if not plantilla:
        return ""
        
    mensaje_final = plantilla
    
    try:
        # Reemplazo manual seguro. Si una variable falta en el diccionario, 
        # simplemente no rompe el sistema y formatea el resto con éxito.
        for clave, valor in datos.items():
            marcador = f"{{{clave}}}"
            if marcador in mensaje_final:
                mensaje_final = mensaje_final.replace(marcador, str(valor))
                
        return mensaje_final
    except Exception as e:
        print(f"❌ Error formateando mensaje: {e}")
        return plantilla