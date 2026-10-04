"""Editar la ficha no debe fallar por el serial de ONU guardado en la MAC."""

import pytest

from src.application.services.client_service import mac_editada
from src.domain.schemas import ClienteUpdate


def test_el_formulario_puede_mandar_el_serial_viejo_sin_cambios():
    # Así venía la ficha de Gabriela: la API ya no responde 422.
    datos = ClienteUpdate(nombre="Gabriela Vazquez Gomez", telefono="9611172026", mac_address="HWTCC099CCAC")
    assert mac_editada(datos.mac_address, "HWTCC099CCAC") == (False, None)


def test_una_mac_nueva_se_normaliza():
    assert mac_editada("aa-bb-cc-dd-ee-ff", "HWTCC099CCAC") == (True, "AA:BB:CC:DD:EE:FF")


def test_borrar_la_mac():
    assert mac_editada("", "AA:BB:CC:DD:EE:FF") == (True, None)


def test_un_valor_nuevo_invalido_explica_el_campo():
    with pytest.raises(ValueError, match="MAC WAN/CPE: .*12 dígitos"):
        mac_editada("hola", None)
