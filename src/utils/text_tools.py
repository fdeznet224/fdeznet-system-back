import re
import unicodedata

def limpiar_string_para_usuario(texto: str) -> str:
    """
    Transforma el nombre real en un usuario válido para MikroTik.
    Ej: "Arisel Fernandez Canaveral" -> "Arisel_Fernandez_Canaveral"
    """
    if not texto: 
        return ""
    
    # 1. Quitar espacios sobrantes al inicio y final
    texto = texto.strip()
    
    # 2. Quitar tildes (Recomendado para evitar problemas en routers)
    # Ej: "García" -> "Garcia"
    texto = ''.join(c for c in unicodedata.normalize('NFD', texto) if unicodedata.category(c) != 'Mn')
    
    # 3. Reemplazar espacios (uno o varios) por UN guion bajo
    # Ej: "Juan    Perez" -> "Juan_Perez"
    texto = re.sub(r'\s+', '_', texto)
    
    # 4. Eliminar cualquier caracter que no sea letra, número o guion bajo
    # Esto evita comillas, paréntesis, etc.
    texto = re.sub(r'[^a-zA-Z0-9_]', '', texto)
    
    return texto

# Esta función también la necesitas para tu Repositorio (si la usaste en el código anterior)
def generar_password_pppoe(longitud=6) -> str:
    """
    Genera una contraseña simple si se necesita.
    """
    import secrets
    import string
    caracteres = string.ascii_letters + string.digits
    return ''.join(secrets.choice(caracteres) for _ in range(longitud))