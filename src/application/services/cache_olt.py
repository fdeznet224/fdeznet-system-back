"""Caché corto de las lecturas completas de una OLT.

Leer una OLT (API de VSOL o SNMP) descarga todas sus ONU: inicio de sesión y
varias peticiones por puerto PON. Si varias pantallas piden la misma OLT a la
vez (potencia en vivo de varios clientes, Radar, la lectura de cada hora), se
hace una sola lectura y todas la comparten. Así la OLT recibe, como mucho,
una lectura completa cada TTL segundos.
"""

import asyncio
import copy
import time
from typing import Any, Awaitable, Callable

TTL_SEGUNDOS = 30

_datos: dict[str, tuple[float, Any]] = {}
_candados: dict[str, asyncio.Lock] = {}


async def lectura_compartida(clave: str, leer: Callable[[], Awaitable[Any]], ttl: float = TTL_SEGUNDOS) -> Any:
    """Devuelve la lectura guardada si es reciente; si no, lee una sola vez.

    Se devuelve una copia para que quien la use pueda modificarla sin tocar
    la de los demás.
    """
    guardado = _datos.get(clave)
    if guardado and time.monotonic() - guardado[0] < ttl:
        return copy.deepcopy(guardado[1])
    candado = _candados.setdefault(clave, asyncio.Lock())
    async with candado:
        # Mientras se esperaba el candado, otra petición pudo haberla leído.
        guardado = _datos.get(clave)
        if guardado and time.monotonic() - guardado[0] < ttl:
            return copy.deepcopy(guardado[1])
        valor = await leer()
        _datos[clave] = (time.monotonic(), valor)
        return copy.deepcopy(valor)


def olvidar(clave_prefijo: str) -> None:
    """Descarta lo guardado (por ejemplo, después de reiniciar una ONU)."""
    for clave in [c for c in _datos if c.startswith(clave_prefijo)]:
        _datos.pop(clave, None)
