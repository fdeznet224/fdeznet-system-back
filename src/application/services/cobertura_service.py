"""A qué zona pertenece la colonia que dice un interesado, y qué plan pidió.

Cada zona tiene su nombre y, opcionalmente, la lista de colonias que cubre
(se edita en Zonas). Así cada ISP define su cobertura sin tocar código.
"""

import re
import unicodedata
from typing import Optional, Sequence

from sqlalchemy import select

from src.infrastructure.models import PlanModel, ZonaModel


def normalizar(texto: Optional[str]) -> str:
    sin_acentos = unicodedata.normalize("NFD", texto or "")
    sin_acentos = "".join(c for c in sin_acentos if unicodedata.category(c) != "Mn")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", sin_acentos.lower()).split())


def nombres_de_zona(zona) -> list[str]:
    """El nombre de la zona y sus colonias, normalizados."""
    colonias = re.split(r"[,;\n]+", getattr(zona, "colonias", None) or "")
    return [n for n in (normalizar(t) for t in [zona.nombre, *colonias]) if n]


def elegir_zona(zonas: Sequence, *textos: Optional[str]):
    """La zona cuya colonia aparece en lo que dijo el cliente (la coincidencia más larga)."""
    mejor, largo = None, 0
    for texto in textos:
        dicho = f" {normalizar(texto)} "
        if not dicho.strip():
            continue
        for zona in zonas:
            for nombre in nombres_de_zona(zona):
                if f" {nombre} " in dicho and len(nombre) > largo:
                    mejor, largo = zona, len(nombre)
        if mejor:
            return mejor
    return None


def elegir_plan(planes: Sequence, texto: Optional[str]):
    """El plan por su nombre ("Estándar") o por su precio ("el de 300")."""
    dicho = normalizar(texto)
    if not dicho:
        return None
    por_nombre = [p for p in planes if normalizar(p.nombre) and f" {normalizar(p.nombre)} " in f" {dicho} "]
    if por_nombre:
        return max(por_nombre, key=lambda p: len(p.nombre or ""))
    numeros = {int(n) for n in re.findall(r"\d+", dicho)}
    return next((p for p in planes if p.precio is not None and int(p.precio) in numeros), None)


async def zona_y_plan(db, colonia: Optional[str], direccion: Optional[str], plan: Optional[str]):
    zonas = (await db.execute(select(ZonaModel).order_by(ZonaModel.id))).scalars().all()
    zona = elegir_zona(zonas, colonia, direccion)
    if not zona or not zona.router_id:
        return zona, None
    planes = (await db.execute(select(PlanModel).where(PlanModel.router_id == zona.router_id))).scalars().all()
    return zona, elegir_plan(planes, plan)
