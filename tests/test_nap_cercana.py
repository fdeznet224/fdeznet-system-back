from src.application.services.nap_service import (
    distancia_metros,
    ordenar_por_cercania,
    parsear_coordenadas,
)
from src.main import app


def test_parsea_coordenadas_de_texto_y_enlace():
    assert parsear_coordenadas("16.7521, -93.1154") == (16.7521, -93.1154)
    assert parsear_coordenadas(
        "https://maps.google.com/?q=16.7521,-93.1154"
    ) == (16.7521, -93.1154)
    assert parsear_coordenadas("Poste 54") is None
    assert parsear_coordenadas("0, 0") is None
    assert parsear_coordenadas(None) is None


def test_distancia_haversine_aproximada():
    # 0.001° de latitud ≈ 111 m
    d = distancia_metros((16.75, -93.11), (16.751, -93.11))
    assert 105 < d < 117


def test_ordena_por_distancia_y_manda_llenas_al_final():
    origen = (16.75, -93.11)
    candidatas = [
        {"id": 1, "posicion": (16.7505, -93.11), "puertos_libres": 0},
        {"id": 2, "posicion": (16.752, -93.11), "puertos_libres": 4},
        {"id": 3, "posicion": (16.751, -93.11), "puertos_libres": 2},
        {"id": 4, "posicion": None, "puertos_libres": 8},
    ]
    orden = ordenar_por_cercania(origen, candidatas)
    assert [c["id"] for c in orden] == [3, 2, 1]
    assert orden[0]["distancia_m"] < orden[1]["distancia_m"]


def test_ruta_cercanas_no_choca_con_id():
    rutas = [r.path for r in app.routes]
    assert "/infraestructura/naps/cercanas" in rutas
    params = {
        p["name"]
        for p in app.openapi()["paths"]["/infraestructura/naps/cercanas"]["get"]["parameters"]
    }
    assert {"latitud", "longitud", "zona_id", "olt_id", "limite"} <= params
