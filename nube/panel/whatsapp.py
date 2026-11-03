"""WhatsApp de ventas del panel: el mismo bot de los ISP, en su propio contenedor.

Con él les llegan a los interesados su acceso al demo y los avisos de su fin.
Lo que contesten lo ven en el celular de ese número; el panel no lo guarda.
"""
import io
import os

import httpx
import qrcode
import qrcode.image.svg

BOT_URL = os.getenv("PANEL_BOT_URL", "http://bot:3000").rstrip("/")
SECRETO = os.getenv("PANEL_BOT_SECRETO", "")


class ErrorWhatsApp(Exception):
    pass


def estado() -> dict:
    """{conectado, iniciado, qr} — qr es el texto del código mientras no esté vinculado."""
    try:
        datos = httpx.get(f"{BOT_URL}/status", timeout=5).json()
    except (httpx.HTTPError, ValueError):
        return {"disponible": False, "conectado": False, "iniciado": False, "qr": None}
    return {"disponible": True, "conectado": bool(datos.get("connected")),
            "iniciado": bool(datos.get("active")), "qr": datos.get("qr")}


def qr_svg(texto: str) -> str:
    imagen = qrcode.make(texto, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    salida = io.BytesIO()
    imagen.save(salida)
    return salida.getvalue().decode()


def enviar(numero: str, mensaje: str) -> None:
    try:
        respuesta = httpx.post(f"{BOT_URL}/enviar-mensaje", json={"numero": numero, "mensaje": mensaje}, timeout=60)
    except httpx.HTTPError as error:
        raise ErrorWhatsApp(f"El WhatsApp de ventas no respondió: {error}") from error
    if respuesta.status_code >= 400:
        try:
            detalle = respuesta.json().get("error")
        except ValueError:
            detalle = None
        raise ErrorWhatsApp(detalle or f"El WhatsApp de ventas respondió {respuesta.status_code}")


def cerrar_sesion() -> None:
    """Desvincula el número y deja el bot listo para mostrar un código nuevo."""
    try:
        httpx.post(f"{BOT_URL}/logout", timeout=30)
        httpx.post(f"{BOT_URL}/init", timeout=10)
    except httpx.HTTPError as error:
        raise ErrorWhatsApp(f"El WhatsApp de ventas no respondió: {error}") from error
