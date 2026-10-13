"""Atenuación: cada cliente contra su propia señal normal y grupos que bajan juntos."""

from datetime import datetime, timedelta

from src.application.services.atenuacion_service import agrupar, cambios_por_servicio, coordenadas, nivel_de
from src.application.services.senal_optica_service import numero_pon

AHORA = datetime(2026, 10, 13, 15, 0)


def _historial(servicio_id, normal, actual, pon=4):
    """Una lectura por hora: 6 días en su normal y las últimas 2 horas en actual."""
    lecturas = [(servicio_id, normal, AHORA - timedelta(days=dia, hours=hora), 7, pon) for dia in range(2, 7) for hora in range(0, 24, 4)]
    lecturas += [(servicio_id, actual, AHORA - timedelta(hours=hora), 7, pon) for hora in range(0, 2)]
    return lecturas


def test_cada_cliente_contra_su_propia_senal_normal():
    # Red desbalanceada: uno en -15 y otro en -24 por diseño; ninguno bajó.
    cambios = cambios_por_servicio(_historial(1, -15.0, -15.2) + _historial(2, -24.0, -24.1), AHORA)
    assert cambios[1]["nivel"] == "estable" and cambios[2]["nivel"] == "estable"


def test_una_baja_de_3_db_es_fuerte_y_guarda_su_pon():
    cambio = cambios_por_servicio(_historial(1, -20.0, -23.4), AHORA)[1]
    assert cambio["cambio_db"] == -3.4 and cambio["nivel"] == "bajo_fuerte"
    assert cambio["normal"] == -20.0 and cambio["actual"] == -23.4 and cambio["pon"] == 4


def test_sin_suficientes_lecturas_normales_no_se_juzga():
    lecturas = [(1, -20.0, AHORA - timedelta(days=2), 7, 4), (1, -25.0, AHORA, 7, 4)]
    assert cambios_por_servicio(lecturas, AHORA) == {}


def test_una_lectura_rara_no_cuenta_como_baja():
    lecturas = _historial(1, -20.0, -20.0)
    lecturas.append((1, -30.0, AHORA - timedelta(hours=2, minutes=30), 7, 4))
    assert cambios_por_servicio(lecturas, AHORA)[1]["nivel"] == "estable"


def test_el_grupo_se_atenua_si_bajaron_varios_a_la_vez():
    miembros = [{"cliente_id": i, "cambio_db": c, "ubicacion": None} for i, c in enumerate([-2.5, -3.1, -2.0, -0.2])]
    grupo = agrupar(miembros)
    assert grupo["bajaron"] == 3 and grupo["medidos"] == 4
    assert grupo["cambio_mediano_db"] == -2.5 and grupo["peor_db"] == -3.1


def test_un_solo_cliente_que_baja_no_es_problema_de_la_troncal():
    miembros = [{"cliente_id": i, "cambio_db": c, "ubicacion": None} for i, c in enumerate([-4.0, -0.1, 0.2, -0.3])]
    assert agrupar(miembros) is None


def test_nivel_coordenadas_y_pon():
    assert [nivel_de(c) for c in (-0.5, -1.5, -3.0)] == ["estable", "bajo", "bajo_fuerte"]
    assert coordenadas("16.395358, -92.693243") == (16.395358, -92.693243)
    assert coordenadas("") is None
    assert [numero_pon(v) for v in ("4", 4, "GPON0/4", None)] == [4, 4, 4, None]
