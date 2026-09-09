import asyncio

from src.application.services.branding_service import (
    get_or_create_system_config,
    initial_branding_values,
)


def test_identidad_inicial_proviene_de_la_instalacion(monkeypatch):
    monkeypatch.setenv("FDEZNET_BRAND_NAME", "ISP Montaña")
    monkeypatch.setenv("FDEZNET_BRAND_SYSTEM_NAME", "Portal Montaña")
    monkeypatch.setenv("FDEZNET_BRAND_EMAIL", "soporte@montana.test")

    values = initial_branding_values()

    assert values == {
        "empresa_nombre": "ISP Montaña",
        "sistema_nombre": "Portal Montaña",
        "empresa_email": "soporte@montana.test",
    }


def test_primera_configuracion_nace_con_marca_blanca(monkeypatch):
    monkeypatch.setenv("FDEZNET_BRAND_NAME", "Fibra Centro")
    monkeypatch.setenv("FDEZNET_BRAND_SYSTEM_NAME", "Fibra Centro")
    monkeypatch.setenv("FDEZNET_BRAND_EMAIL", "hola@fibra-centro.test")

    class FakeDB:
        def __init__(self):
            self.created = None

        async def get(self, _model, _identifier):
            return None

        def add(self, value):
            self.created = value

        async def commit(self):
            return None

        async def refresh(self, _value):
            return None

    db = FakeDB()
    config = asyncio.run(get_or_create_system_config(db))

    assert config is db.created
    assert config.empresa_nombre == "Fibra Centro"
    assert config.sistema_nombre == "Fibra Centro"
    assert config.empresa_email == "hola@fibra-centro.test"
