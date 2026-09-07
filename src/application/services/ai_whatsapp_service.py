import os
from datetime import datetime, time
from zoneinfo import ZoneInfo

import httpx


TZ = ZoneInfo("America/Mexico_City")


def esta_fuera_de_horario(ahora: datetime | None = None) -> bool:
    local = ahora.astimezone(TZ) if ahora and ahora.tzinfo else (ahora or datetime.now(TZ))
    dia = local.weekday()
    hora = local.time()
    if dia <= 4:
        return not (time(8, 0) <= hora < time(20, 0))
    if dia == 5:
        return not (time(9, 0) <= hora < time(14, 0))
    return True


class AIWhatsAppService:
    def __init__(self):
        self.api_key = os.getenv("OPENAI_API_KEY", "").strip()
        self.model = os.getenv("OPENAI_WHATSAPP_MODEL", "gpt-5-mini")
        self.transcription_model = os.getenv(
            "OPENAI_TRANSCRIPTION_MODEL", "gpt-4o-mini-transcribe"
        )

    @property
    def disponible(self) -> bool:
        return bool(self.api_key)

    def _headers(self):
        return {"Authorization": f"Bearer {self.api_key}"}

    async def transcribir(self, media_url: str) -> str:
        async with httpx.AsyncClient(timeout=45) as client:
            media = await client.get(media_url)
            media.raise_for_status()
            respuesta = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers=self._headers(),
                data={"model": self.transcription_model, "language": "es"},
                files={"file": ("nota.ogg", media.content, media.headers.get("content-type", "audio/ogg"))},
            )
            respuesta.raise_for_status()
            return respuesta.json().get("text", "").strip()

    async def responder(self, mensaje: str, cliente=None) -> str:
        contexto = "Cliente no identificado por el teléfono."
        if cliente:
            contexto = (
                f"Cliente: {cliente.nombre}. Estado: {cliente.estado}. "
                f"Saldo a favor: ${cliente.saldo_a_favor or 0}. "
                "No reveles credenciales, IP, cédula ni datos técnicos sensibles."
            )
        instrucciones = (
            "Eres FdezBot, agente nocturno de un proveedor de internet. Responde en español, "
            "breve, amable y sin inventar. Sólo orientas y consultas el contexto entregado. "
            "Nunca afirmes haber registrado pagos, promesas, cambios, cortes o reconexiones. "
            "Para saldo detallado, pagos o promesas indica al cliente que escriba 'fdezbot' y "
            "use el menú seguro. Si hay una falla técnica, recopila una descripción y avisa que "
            "un asesor continuará en horario laboral. " + contexto
        )
        async with httpx.AsyncClient(timeout=30) as client:
            respuesta = await client.post(
                "https://api.openai.com/v1/responses",
                headers={**self._headers(), "Content-Type": "application/json"},
                json={
                    "model": self.model,
                    "instructions": instrucciones,
                    "input": mensaje[:4000],
                    "max_output_tokens": 300,
                    "store": False,
                },
            )
            respuesta.raise_for_status()
            datos = respuesta.json()
            for item in datos.get("output", []):
                for content in item.get("content", []):
                    if content.get("type") == "output_text":
                        return content.get("text", "").strip()
        return "No pude responder en este momento. Un asesor revisará tu mensaje."
