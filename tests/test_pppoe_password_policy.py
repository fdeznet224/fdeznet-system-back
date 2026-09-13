import re

import pytest
from pydantic import ValidationError

from src.application.services.pppoe_config_service import normalizar_config_pppoe
from src.domain.schemas import PppoePasswordConfig
from src.utils.text_tools import generar_password_pppoe


@pytest.mark.parametrize(
    ("tipo", "patron"),
    [
        ("numeros", r"[0-9]+"),
        ("letras", r"[A-Za-z]+"),
        ("alfanumerica", r"[A-Za-z0-9]+"),
    ],
)
def test_password_aleatoria_respeta_longitud_y_tipo(tipo, patron):
    password = generar_password_pppoe(18, tipo)

    assert len(password) == 18
    assert re.fullmatch(patron, password)


def test_password_alfanumerica_incluye_letras_y_numeros():
    password = generar_password_pppoe(12, "alfanumerica")

    assert re.search(r"[A-Za-z]", password)
    assert re.search(r"[0-9]", password)


@pytest.mark.parametrize("longitud", [5, 65])
def test_password_aleatoria_rechaza_longitud_fuera_de_rango(longitud):
    with pytest.raises(ValueError):
        generar_password_pppoe(longitud)


def test_configuracion_anterior_con_password_conserva_modo_fijo():
    config = normalizar_config_pppoe({"pppoe_password_default": "clave-anterior"})

    assert config["modo"] == "fija"
    assert config["password"] == "clave-anterior"


def test_configuracion_nueva_aleatoria_normaliza_valores_invalidos():
    config = normalizar_config_pppoe(
        {
            "pppoe_password_mode": "aleatoria",
            "pppoe_password_length": "100",
            "pppoe_password_charset": "desconocido",
        }
    )

    assert config == {
        "modo": "aleatoria",
        "password": None,
        "longitud": 64,
        "tipo_caracteres": "alfanumerica",
    }


def test_esquema_exige_password_cuando_el_modo_es_fijo():
    with pytest.raises(ValidationError):
        PppoePasswordConfig(modo="fija", password="")


def test_esquema_permite_configuracion_aleatoria_sin_password():
    config = PppoePasswordConfig(
        modo="aleatoria",
        longitud=10,
        tipo_caracteres="numeros",
    )

    assert config.password is None
