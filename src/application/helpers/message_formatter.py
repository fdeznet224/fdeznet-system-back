import re
from typing import Dict, Any

def formatear_mensaje(plantilla: str, datos: Dict[str, Any]) -> str:
    """
    Toma una cadena con etiquetas tipo {variable} y las reemplaza 
    con los valores del diccionario de datos.
    """
    if not plantilla:
        return ""
        
    try:
        # Usamos .format(**datos) que es la forma estándar de Python
        # Pero manejamos errores por si el usuario pone una etiqueta que no existe
        return plantilla.format(**datos)
    except KeyError as e:
        # Si falta una variable, avisamos en consola pero no rompemos el sistema
        print(f"⚠️ Warning: La etiqueta {e} no fue proporcionada en los datos.")
        return plantilla
    except Exception as e:
        print(f"❌ Error formateando mensaje: {e}")
        return plantilla