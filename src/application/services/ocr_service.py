import asyncio
import re
import httpx
import os
import logging
import tempfile
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)
WHATSAPP_UPLOADS = Path(__file__).resolve().parents[3] / "bot_whatsapp" / "uploads"

class OCRService:
    def __init__(self):
        # EasyOCR carga PyTorch y descarga modelos grandes. Se inicializa solo
        # cuando llega el primer comprobante para no retrasar el arranque de la API.
        self._reader = None
        self._reader_lock = asyncio.Lock()

    async def _get_reader(self):
        if self._reader is not None:
            return self._reader
        async with self._reader_lock:
            if self._reader is None:
                import easyocr

                self._reader = await asyncio.to_thread(
                    easyocr.Reader,
                    ["es", "en"],
                )
        return self._reader

    @staticmethod
    def extraer_datos(texto_crudo: str) -> dict:
        """Interpreta el texto OCR sin depender de una imagen real."""
        texto = " ".join((texto_crudo or "").split()).lower()

        folio = None
        patrones_folio = [
            r"clave de rastreo[^\w]*([a-z0-9-]{10,50})",
            r"rastreo es[^\w]*([a-z0-9-]{10,50})",
            r"folio[^\w]*([a-z0-9-]{6,50})",
            r"operaci[oó]n[^\w]*([a-z0-9-]{6,50})",
            r"referencia[^\w]*([a-z0-9-]{6,50})",
            r"ref\.[^\w]*([a-z0-9-]{6,50})",
            r"autorizaci[oó]n[^\w]*([a-z0-9-]{6,50})",
        ]
        for patron in patrones_folio:
            match = re.search(patron, texto)
            if not match:
                continue
            candidato = re.sub(
                r"[^A-Z0-9-]",
                "",
                match.group(1).upper(),
            )
            if any(c.isdigit() for c in candidato):
                folio = candidato
                break

        if not folio:
            candidatos = [
                re.sub(r"[^A-Z0-9-]", "", palabra.upper())
                for palabra in texto_crudo.split()
                if len(palabra) >= 10
                and any(c.isdigit() for c in palabra)
            ]
            candidatos = [
                candidato
                for candidato in candidatos
                if len(candidato) >= 10
            ]
            if candidatos:
                folio = max(candidatos, key=len)

        monto = 0.0
        numero = r"(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?)"
        patrones_monto = [
            rf"(?:importe|monto|enviaste|transferiste|total)[^\d]*{numero}",
            rf"[\$sS]\s*{numero}",
            rf"{numero}\s*(?:mxn|m\.?n\.?|pesos)",
        ]
        for patron in patrones_monto:
            match = re.search(patron, texto)
            if not match:
                continue
            try:
                monto = float(match.group(1).replace(",", ""))
            except ValueError:
                continue
            if monto > 0:
                break

        cedula_detectada = None
        for patron in [
            r"concepto[^\w]*([a-z0-9]{3,10})",
            r"motivo[^\w]*([a-z0-9]{3,10})",
            r"mensaje[^\w]*([a-z0-9]{3,10})",
            r"descripci[oó]n[^\w]*([a-z0-9]{3,10})",
        ]:
            match = re.search(patron, texto)
            if match:
                cedula_detectada = match.group(1).upper()
                break

        return {
            "folio": folio,
            "monto": monto,
            "cedula_detectada": cedula_detectada,
            "exito": folio is not None and monto > 0,
        }

    async def procesar_ticket(self, url_imagen: str):
        """Descarga una imagen y extrae folio, monto y número de contrato."""
        temp_path = None

        try:
            parsed = urlparse(url_imagen)
            content_type = ""
            if parsed.scheme == "whatsapp-media":
                name = parsed.netloc or Path(parsed.path).name
                source = (WHATSAPP_UPLOADS / name).resolve()
                source.relative_to(WHATSAPP_UPLOADS.resolve())
                if not source.is_file() or source.is_symlink():
                    raise ValueError("El comprobante privado no existe")
                contenido = await asyncio.to_thread(source.read_bytes)
                content_type = "image/" + source.suffix.lower().lstrip(".")
            elif parsed.scheme in {"http", "https"}:
                # Compatibilidad temporal con comprobantes creados antes de que
                # los archivos pasaran a servirse exclusivamente con autenticación.
                async with httpx.AsyncClient(
                    timeout=20.0,
                    follow_redirects=True,
                ) as client:
                    resp = await client.get(url_imagen)
                    resp.raise_for_status()
                    contenido = resp.content
                    content_type = resp.headers.get("content-type", "").lower()
            else:
                raise ValueError("URL de comprobante inválida")

            if not contenido or len(contenido) > 10 * 1024 * 1024:
                raise ValueError("Imagen vacía o mayor a 10 MB")

            if content_type and not content_type.startswith("image/"):
                raise ValueError("El comprobante recibido no es una imagen")

            with tempfile.NamedTemporaryFile(
                prefix="fdeznet_ticket_",
                suffix=".jpg",
                delete=False,
            ) as temporal:
                temporal.write(contenido)
                temp_path = temporal.name

            reader = await self._get_reader()
            resultados = await asyncio.to_thread(
                reader.readtext,
                temp_path,
                detail=0,
            )
            texto_crudo = " ".join(resultados)
            # No registrar el texto completo: puede contener datos bancarios,
            # nombres, cuentas o referencias personales.
            logger.info("OCR procesado; se extrajeron datos estructurados")
            return self.extraer_datos(texto_crudo)

        except Exception as e:
            logger.error(f"Error procesando OCR: {e}")
            return {"folio": None, "monto": 0.0, "cedula_detectada": None, "exito": False}
            
        finally:
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)
