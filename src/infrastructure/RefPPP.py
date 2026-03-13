import unicodedata
import re

def generar_formato(nombre_completo: str) -> str:
    """
    Genera un usuario PPPoE estandarizado.
    Input: "Arisel Fernández Cañaveral"
    Output: "Arisel_Fernandez_Canaveral"
    """
    if not nombre_completo:
        return "usuario_nuevo"

    # 1. Normalizar: Separa tildes de las letras (ñ -> n~)
    # Esto convierte "á" en "a" + "´"
    texto_normalizado = unicodedata.normalize('NFKD', nombre_completo)
    
    # 2. Filtrar: Nos quedamos solo con caracteres ASCII (sin tildes)
    # encode('ASCII', 'ignore') descarta lo que no sea estándar
    texto_sin_tildes = texto_normalizado.encode('ASCII', 'ignore').decode('utf-8')

    # 3. Limpieza Estricta: Solo letras, números y espacios
    # Eliminamos símbolos raros (@, %, -, etc.)
    solo_alfanumerico = re.sub(r'[^a-zA-Z0-9\s]', '', texto_sin_tildes)

    # 4. Formateo Final:
    # - strip(): Quita espacios al inicio/final
    # - title(): Pone Mayúsculas Iniciales (Arisel Fernandez)
    # - replace(): Cambia espacios internos por guiones bajos
    usuario_final = solo_alfanumerico.strip().title().replace(" ", "_")
    
    # Limpieza extra por si quedaron guiones dobles (Arisel__Fernandez)
    while "__" in usuario_final:
        usuario_final = usuario_final.replace("__", "_")

    return usuario_final