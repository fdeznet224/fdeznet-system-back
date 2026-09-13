from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.models import ConfiguracionModel
from src.utils.text_tools import generar_password_pppoe


PPPOE_CONFIG_KEYS = {
    "modo": "pppoe_password_mode",
    "password": "pppoe_password_default",
    "longitud": "pppoe_password_length",
    "tipo_caracteres": "pppoe_password_charset",
}
MODOS_VALIDOS = {"fija", "aleatoria"}
TIPOS_VALIDOS = {"numeros", "letras", "alfanumerica"}


def normalizar_config_pppoe(valores: dict[str, str]) -> dict:
    password = (valores.get(PPPOE_CONFIG_KEYS["password"]) or "").strip()
    modo_guardado = (valores.get(PPPOE_CONFIG_KEYS["modo"]) or "").strip()
    modo = modo_guardado if modo_guardado in MODOS_VALIDOS else (
        "fija" if password else "aleatoria"
    )
    tipo = (
        valores.get(PPPOE_CONFIG_KEYS["tipo_caracteres"], "alfanumerica")
        .strip()
        .lower()
    )
    if tipo not in TIPOS_VALIDOS:
        tipo = "alfanumerica"
    try:
        longitud = int(valores.get(PPPOE_CONFIG_KEYS["longitud"], "12"))
    except (TypeError, ValueError):
        longitud = 12
    longitud = min(64, max(6, longitud))
    return {
        "modo": modo,
        "password": password if modo == "fija" else None,
        "longitud": longitud,
        "tipo_caracteres": tipo,
    }


async def obtener_config_pppoe(db: AsyncSession) -> dict:
    claves = set(PPPOE_CONFIG_KEYS.values())
    registros = (
        await db.execute(
            select(ConfiguracionModel).where(ConfiguracionModel.clave.in_(claves))
        )
    ).scalars().all()
    return normalizar_config_pppoe(
        {registro.clave: registro.valor or "" for registro in registros}
    )


async def guardar_config_pppoe(db: AsyncSession, datos) -> dict:
    config = {
        "modo": datos.modo,
        "password": (datos.password or "").strip(),
        "longitud": datos.longitud,
        "tipo_caracteres": datos.tipo_caracteres,
    }
    valores = {
        PPPOE_CONFIG_KEYS["modo"]: config["modo"],
        PPPOE_CONFIG_KEYS["password"]: config["password"],
        PPPOE_CONFIG_KEYS["longitud"]: str(config["longitud"]),
        PPPOE_CONFIG_KEYS["tipo_caracteres"]: config["tipo_caracteres"],
    }
    existentes = (
        await db.execute(
            select(ConfiguracionModel).where(
                ConfiguracionModel.clave.in_(set(valores))
            )
        )
    ).scalars().all()
    por_clave = {registro.clave: registro for registro in existentes}
    for clave, valor in valores.items():
        if clave in por_clave:
            por_clave[clave].valor = valor
        else:
            db.add(ConfiguracionModel(clave=clave, valor=valor))
    await db.commit()
    return {
        **config,
        "password": config["password"] if config["modo"] == "fija" else None,
    }


async def resolver_password_pppoe(db: AsyncSession) -> str:
    config = await obtener_config_pppoe(db)
    if config["modo"] == "fija":
        if not config["password"]:
            raise ValueError("Configura la contraseña PPPoE fija antes de crear clientes")
        return config["password"]
    return generar_password_pppoe(
        config["longitud"],
        config["tipo_caracteres"],
    )
