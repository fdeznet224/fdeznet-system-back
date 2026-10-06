import pytest

from src.application.services import cache_olt


@pytest.fixture(autouse=True)
def _cache_olt_vacio():
    """Cada prueba empieza sin lecturas de OLT guardadas por otra."""
    cache_olt._datos.clear()
    cache_olt._candados.clear()
    yield
    cache_olt._datos.clear()
    cache_olt._candados.clear()
