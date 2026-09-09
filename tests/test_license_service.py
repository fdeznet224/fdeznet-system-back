from src.application.services.license_service import update_available


def test_detecta_actualizacion_semantica_disponible():
    assert update_available("2.3.0", "2.4.0") is True
    assert update_available("2.3.0", "2.3.1") is True


def test_no_marca_version_igual_o_anterior():
    assert update_available("2.3.0", "2.3.0") is False
    assert update_available("2.3.0", "2.2.9") is False
    assert update_available("2.3.0", None) is False


def test_version_invalida_no_provoca_actualizacion():
    assert update_available("desarrollo", "2.4.0") is False
