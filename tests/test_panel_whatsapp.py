"""El panel le habla a su bot de ventas con el secreto compartido."""
import httpx
import pytest

from nube.panel import whatsapp


@pytest.fixture
def bot(monkeypatch):
    pedidos = []
    monkeypatch.setattr(whatsapp, "SECRETO", "secreto-del-bot")

    def responder(peticion):
        pedidos.append(peticion)
        if peticion.headers.get("X-Webhook-Secret") != "secreto-del-bot":
            return httpx.Response(401, json={"error": "No autorizado"})
        if peticion.url.path == "/status":
            return httpx.Response(200, json={"active": True, "connected": False, "qr": "2@abc"})
        return httpx.Response(200, json={"ok": True})

    cliente = httpx.Client(transport=httpx.MockTransport(responder))
    monkeypatch.setattr(whatsapp.httpx, "get", cliente.get)
    monkeypatch.setattr(whatsapp.httpx, "post", cliente.post)
    return pedidos


def test_estado_y_envio_llevan_el_secreto(bot):
    assert whatsapp.estado() == {"disponible": True, "conectado": False, "iniciado": True, "qr": "2@abc"}
    whatsapp.enviar("529611112233", "Hola")
    assert [p.url.path for p in bot] == ["/status", "/enviar-mensaje"]
    assert "<svg" in whatsapp.qr_svg("2@abc")


def test_sin_secreto_el_bot_no_cuenta_como_disponible(bot, monkeypatch):
    monkeypatch.setattr(whatsapp, "SECRETO", "otro")
    assert whatsapp.estado()["disponible"] is False
    with pytest.raises(whatsapp.ErrorWhatsApp):
        whatsapp.enviar("529611112233", "Hola")
