"""
Cómo entra el técnico a la página de la ONU estando en el WiFi del cliente:
dirección y cuentas por marca. Cada ISP pone las suyas en Configuración; sin
nada guardado se muestran las de fábrica de Huawei.
"""

from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.models import ConfiguracionModel

MARCAS = ("huawei", "zte")
PREDETERMINADO = {
    "huawei": {"url": "http://192.168.100.1", "cuentas": "root:admin,telecomadmin:admintelecom"},
    "zte": {"url": "http://192.168.1.1", "cuentas": ""},
}
# configuracion.valor es de 100 caracteres.
LARGO_MAXIMO = 100
# Puerto web del router de los clientes por radio (Mercusys con administración
# remota en la WAN). Vacío: el clic en la IP abre el puerto 80.
CLAVE_PUERTO_RADIO = "router_radio_puerto"
PUERTO_RADIO_PREDETERMINADO = "8080"


def _clave(marca: str, campo: str) -> str:
    return f"onu_acceso_{marca}_{campo}"


def marca_de_onu(identificador: Optional[str], modelo: Optional[str] = None) -> Optional[str]:
    """HWTC… / HG8145 → huawei; ZTEG… / F670L → zte; una MAC EPON no dice la marca."""
    serie = (identificador or "").upper()
    texto = (modelo or "").upper()
    if serie.startswith("HWTC") or "HUAWEI" in texto or texto.startswith("HG8") or texto.startswith("EG8"):
        return "huawei"
    if serie.startswith("ZTEG") or "ZTE" in texto or texto.startswith("F6"):
        return "zte"
    return None


def cuentas_de(texto: str) -> list[dict]:
    """"usuario:clave,usuario2:clave2" → [{usuario, clave}, ...]."""
    cuentas = []
    for par in (texto or "").split(","):
        usuario, _, clave = par.strip().partition(":")
        if usuario.strip():
            cuentas.append({"usuario": usuario.strip(), "clave": clave.strip()})
    return cuentas


async def obtener_acceso_onu(db: AsyncSession) -> dict:
    claves = [_clave(m, c) for m in MARCAS for c in ("url", "cuentas")] + [CLAVE_PUERTO_RADIO]
    guardado = {
        r.clave: r.valor
        for r in (
            await db.execute(select(ConfiguracionModel).where(ConfiguracionModel.clave.in_(claves)))
        ).scalars().all()
    }
    return {
        **{
            marca: {
                campo: guardado.get(_clave(marca, campo)) if _clave(marca, campo) in guardado else PREDETERMINADO[marca][campo]
                for campo in ("url", "cuentas")
            }
            for marca in MARCAS
        },
        "radio": {"puerto": guardado.get(CLAVE_PUERTO_RADIO, PUERTO_RADIO_PREDETERMINADO)},
    }


async def guardar_acceso_onu(db: AsyncSession, datos: dict) -> dict:
    valores = {}
    for marca in MARCAS:
        for campo in ("url", "cuentas"):
            valor = str((datos.get(marca) or {}).get(campo) or "").strip()
            if len(valor) > LARGO_MAXIMO:
                raise ValueError(f"El campo {campo} de {marca.upper()} pasa de {LARGO_MAXIMO} caracteres")
            if campo == "url" and valor and not valor.startswith(("http://", "https://")):
                valor = f"http://{valor}"
            valores[_clave(marca, campo)] = valor
    puerto = str((datos.get("radio") or {}).get("puerto") or "").strip()
    if puerto and not (puerto.isdigit() and 1 <= int(puerto) <= 65535):
        raise ValueError("El puerto del router de radio debe ser un número de 1 a 65535")
    valores[CLAVE_PUERTO_RADIO] = puerto
    existentes = {
        r.clave: r
        for r in (
            await db.execute(select(ConfiguracionModel).where(ConfiguracionModel.clave.in_(set(valores))))
        ).scalars().all()
    }
    for clave, valor in valores.items():
        if clave in existentes:
            existentes[clave].valor = valor
        else:
            db.add(ConfiguracionModel(clave=clave, valor=valor))
    await db.commit()
    return await obtener_acceso_onu(db)


async def acceso_para_tecnico(db: AsyncSession, marca: Optional[str]) -> list[dict]:
    """Las marcas que aplican (la de su ONU, o todas si no se sabe) con sus cuentas."""
    config = await obtener_acceso_onu(db)
    return [
        {"marca": m, "url": config[m]["url"], "cuentas": cuentas_de(config[m]["cuentas"])}
        for m in ([marca] if marca else MARCAS)
        if config[m]["url"]
    ]
