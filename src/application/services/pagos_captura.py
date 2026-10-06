"""Utilidades para validar pagos con la captura de la transferencia.

Antes vivían junto a la lectura del correo del banco. Ese correo dejó de
llegar y se quitó: el pago se valida con la captura (folio único, fecha
reciente, monto y cuenta destino del ISP). Una pasarela de pago (Mercado
Pago, Stripe...) se integrará después por separado.
"""

import re
import unicodedata
from datetime import date, datetime, time

from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.models import ConfiguracionCorreoBancoModel, FacturaModel

# Un pago adelantado se aplica el día 1 del mes que cubre, a esta hora.
HORA_APLICAR_ADELANTADOS = time(8, 0)

# Palabras que traen casi todos los conceptos y no identifican a nadie.
PALABRAS_GENERICAS = {
    "pago", "pagos", "internet", "fdeznet", "transferencia", "envio", "folio",
    "cuenta", "servicio", "mensualidad", "renta", "comision", "incluye", "iva",
    "contactanos", "ayuda", "linea", "azteca", "banco", "tipo", "operacion",
    "concepto", "spei", "abono", "deposito", "mes", "del", "los", "las", "para",
}


def palabras(texto: str | None) -> set[str]:
    """Palabras en minúsculas y sin acentos, para comparar conceptos."""
    plano = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode().lower()
    return set(re.findall(r"[a-z0-9]+", plano))


def contrato_en_concepto(contrato: str | None, texto: set[str]) -> bool:
    """El contrato como palabra ("2e3a") o pegado a letras ("fdeznet2e3a")."""
    contrato = (contrato or "").strip().lower()
    if len(contrato) < 3:
        return False
    return any(
        palabra == contrato
        or (palabra.endswith(contrato) and palabra[: -len(contrato)].isalpha())
        for palabra in texto
    )


def normalize_reference(value: str | None) -> str | None:
    normalized = re.sub(r"[^A-Z0-9]", "", (value or "").upper())
    return normalized if len(normalized) >= 6 else None


def normalize_reference_for_match(value: str | None) -> str | None:
    normalized = normalize_reference(value)
    if not normalized:
        return None
    # El OCR confunde con frecuencia estas letras con dígitos en las claves.
    # Se normaliza la referencia completa; nunca se compara solo un fragmento.
    return normalized.translate(str.maketrans({"O": "0", "I": "1"}))


def aplicar_pago_desde(factura: FacturaModel) -> datetime | None:
    """Día 1 del mes que cubre la factura, o None si no hay fecha."""
    inicio = factura.periodo_desde or factura.fecha_vencimiento
    if not inicio:
        return None
    return datetime.combine(date(inicio.year, inicio.month, 1), HORA_APLICAR_ADELANTADOS)


def cuentas_destino_permitidas(config: ConfiguracionCorreoBancoModel) -> set[str]:
    """Terminaciones (4 dígitos) de las cuentas del ISP que reciben pagos."""
    return {
        item.strip()
        for item in (config.cuentas_destino_permitidas or "").split(",")
        if re.fullmatch(r"[0-9]{4}", item.strip())
    }


async def config_pagos(db: AsyncSession) -> ConfiguracionCorreoBancoModel:
    """Ajustes de la validación por captura (cuentas destino y días válidos).

    Se guardan en la misma fila que usaba la integración de correo para no
    perder lo que el ISP ya configuró.
    """
    config = await db.get(ConfiguracionCorreoBancoModel, 1)
    if config is None:
        config = ConfiguracionCorreoBancoModel(id=1)
        db.add(config)
        await db.flush()
    return config
