"""Teléfonos de clientes en México: en el sistema se guardan los 10 dígitos.

WhatsApp usa 52 + 10 dígitos (cuentas nuevas) o 521 + 10 dígitos (cuentas
antiguas); el puente de WhatsApp prueba las dos al enviar, así que en la ficha
basta con los 10 dígitos.
"""

import re
from typing import Optional

MENSAJE_INVALIDO = "El teléfono debe tener 10 dígitos (ej. 9611234567)"


def _digitos(valor) -> str:
    texto = str(valor if valor is not None else "").strip()
    # Excel entrega los números de una columna con celdas vacías como decimal.
    if re.fullmatch(r"\d+\.0+", texto):
        texto = texto.split(".", 1)[0]
    return re.sub(r"\D", "", texto)


def telefono_10(valor) -> Optional[str]:
    """Los 10 dígitos de un número mexicano escrito en cualquier forma común.

    Acepta "9611234567", "961 123 4567", "961-123-4567", "+52 961...",
    "+52 1 961...", "52961...", "521961...", "044961..." y "0052...".
    Devuelve None si no es un número mexicano de 10 dígitos.
    """
    digitos = _digitos(valor)
    if digitos.startswith("00"):
        digitos = digitos[2:]
    if len(digitos) == 13 and digitos.startswith("521"):
        return digitos[3:]
    if len(digitos) == 12 and digitos.startswith("52"):
        return digitos[2:]
    if len(digitos) == 13 and digitos[:3] in ("044", "045"):
        return digitos[3:]
    if len(digitos) == 10:
        return digitos
    return None


def telefono_cliente(valor) -> Optional[str]:
    """Como se guarda el teléfono de la ficha: 10 dígitos o vacío.

    Un número de otro país se acepta escrito con "+" y su lada (se guarda
    "+" y los dígitos). Cualquier otra cosa es un error.
    """
    texto = str(valor if valor is not None else "").strip()
    if not _digitos(texto):
        return None
    diez = telefono_10(texto)
    if diez:
        return diez
    digitos = _digitos(texto)
    if texto.startswith("+") and 11 <= len(digitos) <= 15 and not digitos.startswith("52"):
        return f"+{digitos}"
    raise ValueError(MENSAJE_INVALIDO)


def telefono_para_whatsapp(valor) -> str:
    """Número para el puente de WhatsApp: 521 + 10 dígitos si es de México."""
    texto = str(valor or "").strip()
    if "@" in texto:
        return texto
    diez = telefono_10(texto)
    return f"521{diez}" if diez else _digitos(texto)
