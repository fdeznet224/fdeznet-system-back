from src.application.services.router_monitor_service import (
    FALLAS_PARA_OFFLINE,
    describir_enlace_vpn,
    evaluar_estado_router,
)


def test_fallas_aisladas_no_marcan_offline():
    online, fallas = True, 0
    for _ in range(FALLAS_PARA_OFFLINE - 1):
        online, fallas = evaluar_estado_router(online, False, fallas)
        assert online is True

    online, fallas = evaluar_estado_router(online, False, fallas)
    assert online is False
    assert fallas == FALLAS_PARA_OFFLINE


def test_una_respuesta_lo_marca_online_y_reinicia_conteo():
    assert evaluar_estado_router(False, True, 7) == (True, 0)
    assert evaluar_estado_router(True, True, 2) == (True, 0)


def test_describe_si_cae_el_tunel_o_solo_la_api():
    ahora = 10_000
    assert describir_enlace_vpn(None, ahora) == ""
    assert "nunca" in describir_enlace_vpn(0, ahora)
    assert "hace 10 min" in describir_enlace_vpn(ahora - 600, ahora)
    assert "sigue activo" in describir_enlace_vpn(ahora - 30, ahora)
