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


class _DBStock(_DB):
    def __init__(self, minimo):
        super().__init__()
        self.config = SimpleNamespace(stock_minimo_onus=minimo)

    async def get(self, _modelo, _llave):
        return self.config


@pytest.mark.parametrize("disponibles, minimo, bajo", [(2, 5, True), (5, 5, False), (0, 0, False)])
def test_avisa_cuando_quedan_menos_onu_que_el_minimo(monkeypatch, disponibles, minimo, bajo):
    async def equipos(self, estado=None):
        assert estado == "DISPONIBLE"
        return [object()] * disponibles

    monkeypatch.setattr(InventarioService, "obtener_equipos", equipos)
    resumen = asyncio.run(InventarioService(_DBStock(minimo)).resumen_stock())
    assert resumen == {"disponibles": disponibles, "minimo": minimo, "bajo": bajo}


def test_las_disponibles_no_se_esconden_por_un_contrato_cancelado():
    db = _DB()

    asyncio.run(InventarioService(db).obtener_equipos("DISPONIBLE"))

    assert "servicios.estado !=" in str(db.consultas[0])


class _DBCambio:
    def __init__(self, onu_nueva):
        self.onu_nueva = onu_nueva
        self.consultas = []
        self.agregados = []

    async def execute(self, consulta):
        self.consultas.append(consulta)
        if len(self.consultas) == 1:
            return SimpleNamespace(scalar_one_or_none=lambda: self.onu_nueva)
        return SimpleNamespace(scalar_one_or_none=lambda: None)

    async def get(self, _modelo, _llave):
        return SimpleNamespace(id=7, estado="INSTALADO", tecnico_id=2)

    def add(self, objeto):
        self.agregados.append(objeto)

    async def flush(self):
        return None


def test_al_cambiar_la_onu_el_contrato_deja_la_retirada():
    from src.application.services.ftth_service import FTTHService

    nueva = SimpleNamespace(id=9, identificador="HWTC00000009", estado="DISPONIBLE", tecnico_id=None)
    cliente = SimpleNamespace(id=40, onu_id=7)
    db = _DBCambio(nueva)

    asyncio.run(FTTHService(db).asignar_onu(cliente, 9, 1, estado_onu_anterior="CON_FALLA"))

    actualizacion = next(c for c in db.consultas if str(c).startswith("UPDATE servicios"))
    parametros = actualizacion.compile().params
    assert parametros["onu_id"] == 9 and 7 in parametros.values() and 40 in parametros.values()
    assert cliente.onu_id == 9
