import asyncio
from types import SimpleNamespace

import pytest

from src.application.services.inventario_service import InventarioService


class _Resultado:
    def __init__(self, fila):
        self.fila = fila

    def scalars(self):
        return self

    def first(self):
        return self.fila

    def all(self):
        return []


class _DB:
    def __init__(self, existente=None):
        self.existente = existente
        self.agregados = []
        self.commits = 0
        self.consultas = []

    async def execute(self, consulta):
        self.consultas.append(consulta)
        return _Resultado(self.existente)

    def add(self, objeto):
        self.agregados.append(objeto)

    async def flush(self):
        return None

    async def commit(self):
        self.commits += 1

    async def refresh(self, _objeto):
        return None


def test_una_onu_dada_de_baja_se_reactiva_al_volver_a_registrarla():
    onu = SimpleNamespace(id=160, identificador="HWTC05450CB6", tecnologia="GPON",
                          modelo="Viejo", estado="BAJA", tecnico_id=3)
    db = _DB(onu)

    resultado = asyncio.run(InventarioService(db).registrar_equipo("hwtc-0545-0cb6", "gpon", "HG8145", usuario_id=1))

    assert resultado is onu
    assert (onu.estado, onu.modelo, onu.tecnico_id) == ("DISPONIBLE", "HG8145", None)
    assert db.commits == 1
    movimiento = db.agregados[0]
    assert (movimiento.estado_anterior, movimiento.estado_nuevo, movimiento.condicion) == ("BAJA", "DISPONIBLE", "REINGRESO")


def test_una_onu_activa_no_se_duplica_y_dice_donde_esta():
    onu = SimpleNamespace(id=5, identificador="HWTC05450CB6", estado="INSTALADO")

    with pytest.raises(ValueError, match="instalada en un cliente"):
        asyncio.run(InventarioService(_DB(onu)).registrar_equipo("HWTC05450CB6", "GPON"))


def test_la_lista_completa_no_muestra_las_dadas_de_baja():
    db = _DB()

    asyncio.run(InventarioService(db).obtener_equipos())

    assert "inventario_onus.estado !=" in str(db.consultas[0])
