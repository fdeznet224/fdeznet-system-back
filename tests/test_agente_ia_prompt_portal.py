"""El agente sabe explicar la app del cliente con la dirección real del portal."""
from src.application.services import agente_ia_service
from src.application.services.agente_ia_prompt import INSTRUCCIONES


def test_instrucciones_traen_la_direccion_del_portal(monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "https://fdezpay.com/")
    texto = INSTRUCCIONES.format(empresa="FdezNet", hoy="hoy", portal=agente_ia_service._url_portal())
    assert "https://fdezpay.com/cliente" in texto
    assert "Primera vez" in texto and "Mi WiFi" in texto and "Mis facturas" in texto


def test_sin_public_url_no_inventa_una_direccion(monkeypatch):
    monkeypatch.delenv("PUBLIC_URL", raising=False)
    assert "pasar_a_humano" in agente_ia_service._url_portal()
