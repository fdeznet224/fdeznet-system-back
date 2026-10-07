import asyncio
from types import SimpleNamespace

from src.application.services.vinculacion_onu_service import VinculacionOnuService, puntaje, sugerir


def _c(id, nombre, cedula=None, pppoe=None):
    return SimpleNamespace(id=id, nombre=nombre, cedula=cedula or f"C{id}", user_pppoe=pppoe)


VICTOR = _c(1, "Víctor Constantino Martínez", "7659", "victor7659")
MARIA_L = _c(2, "María López Hernández", "7710")
MARIA_G = _c(3, "María Gómez Ruiz", "7711")
JOSE = _c(4, "José Luis Pérez", "7720")


def test_la_descripcion_con_el_nombre_apunta_al_cliente():
    assert puntaje("VICTOR_CONSTANTINO", VICTOR) >= 0.85
    assert puntaje("victor constantino mtz", VICTOR) >= 0.5
    assert puntaje("Victor Constantino Matinez", VICTOR) >= 0.85   # error de dedo
    assert puntaje("JOSE LUIS PEREZ", VICTOR) == 0.0


def test_el_contrato_o_el_pppoe_en_la_descripcion_es_seguro():
    assert puntaje("7659", VICTOR) == 1.0
    assert puntaje("victor7659", VICTOR) == 1.0


def test_un_solo_nombre_no_alcanza():
    assert puntaje("Maria", MARIA_L) < 0.5


def test_sugiere_una_onu_por_cliente_y_marca_las_seguras():
    onus = [
        {"identificador": "HWTC0000AAAA", "description": "VICTOR CONSTANTINO"},
        {"identificador": "HWTC0000BBBB", "description": "maria lopez"},
        {"identificador": "HWTC0000CCCC", "description": "MARIA"},          # ambigua
        {"identificador": "HWTC0000DDDD", "description": ""},               # sin descripción
        {"identificador": "HWTC0000EEEE", "description": "victor constantino"},  # repetida
    ]
    resultado = sugerir(onus, [VICTOR, MARIA_L, MARIA_G, JOSE])
    assert resultado["HWTC0000AAAA"]["cliente_id"] == 1 and resultado["HWTC0000AAAA"]["segura"]
    assert resultado["HWTC0000BBBB"]["cliente_id"] == 2
    assert "HWTC0000CCCC" not in resultado and "HWTC0000DDDD" not in resultado
    # Víctor ya quedó con la primera: no se sugiere dos veces.
    assert "HWTC0000EEEE" not in resultado


def test_homonimos_no_son_seguros():
    onus = [{"identificador": "S1", "description": "maria lopez"}]
    gemela = _c(9, "María López Hernández", "7799")
    assert sugerir(onus, [MARIA_L, gemela])["S1"]["segura"] is False


class _DB:
    def __init__(self, objetos):
        self.objetos = objetos
        self.rollbacks = 0

    async def get(self, modelo, llave):
        return self.objetos.get((modelo.__name__, llave))

    async def rollback(self):
        self.rollbacks += 1


def test_vincular_reporta_cada_error_sin_frenar_a_los_demas():
    olt = SimpleNamespace(id=1, tecnologia="GPON")
    con_onu = SimpleNamespace(id=5, nombre="Ana", onu_id=99)
    db = _DB({("OLTModel", 1): olt, ("ClienteModel", 5): con_onu})

    async def lector(_olt_id):
        return {"onus": [{"identificador": "HWTC0000AAAA"}]}

    resultados = asyncio.run(VinculacionOnuService(db, lector=lector).vincular(1, [
        {"identificador": "HWTC0000ZZZZ", "cliente_id": 5},
        {"identificador": "hwtc0000aaaa", "cliente_id": 5},
        {"identificador": "HWTC0000AAAA", "cliente_id": 404},
    ], SimpleNamespace(id=1)))
    assert [r["error"] for r in resultados] == [
        "La OLT ya no reporta esa ONU", "Ana ya tiene una ONU", "El cliente no existe"]
    assert db.rollbacks == 3
