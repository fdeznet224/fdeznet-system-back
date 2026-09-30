import asyncio
import hashlib
import re
from datetime import datetime
import httpx
import os
import logging
import tempfile
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)
WHATSAPP_UPLOADS = Path(__file__).resolve().parents[3] / "bot_whatsapp" / "uploads"

def huella_captura(monto: float, fecha_pago: datetime | None, cuentas: list[str]) -> str | None:
    """Folio propio de una captura sin referencia bancaria.

    Monto + fecha y hora de la transferencia (al segundo) + terminaciones de
    cuenta visibles: si la misma captura se reenvía da la misma huella. Sin
    hora no se genera, porque el mismo monto se repite cada mes.
    """
    if not monto or monto <= 0 or not fecha_pago:
        return None
    base = f"{monto:.2f}|{fecha_pago:%Y%m%d%H%M%S}|{','.join(sorted(set(cuentas)))}"
    return "SC-" + hashlib.sha256(base.encode()).hexdigest()[:16].upper()


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
            r"concepto[^\w]*([a-z0-9]{3,20})",
            r"motivo[^\w]*([a-z0-9]{3,20})",
            r"mensaje[^\w]*([a-z0-9]{3,20})",
            r"descripci[oó]n[^\w]*([a-z0-9]{3,20})",
        ]:
            match = re.search(patron, texto)
            if match:
                # "fdeznet2E3A": el contrato va pegado al nombre de la empresa.
                cedula_detectada = re.sub(r"^fdeznet", "", match.group(1)).upper() or None
                break

        # Fecha y hora de la transferencia (hora local de la captura):
        # "30/09/2026 08.58.12", "30-09-2026 8:58".
        fecha_pago = None
        match = re.search(
            r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})\D{0,20}?(\d{1,2})[:.](\d{2})(?:[:.](\d{2}))?",
            texto,
        )
        if match:
            dia, mes, anio, hora, minuto, segundo = match.groups()
            try:
                fecha_pago = datetime(
                    int(anio), int(mes), int(dia), int(hora), int(minuto), int(segundo or 0)
                )
            except ValueError:
                fecha_pago = None

        # Texto del concepto: "Concepto de transferencia: margarita moreno lopez 30/09/2026".
        concepto = None
        match = re.search(
            r"(?:concepto|motivo)(?: de(?: la)? (?:transferencia|pago|operaci[oó]n))?\s*[:\-]?\s*"
            r"(.{3,80}?)(?=\s+\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\s+(?:fecha|folio|referencia|clave|cuenta|"
            r"importe|monto|compartir|hora|banco|comisi[oó]n)\b|$)",
            texto,
        )
        if match:
            concepto = match.group(1).strip(" :-") or None

        # Terminaciones de cuenta o tarjeta: "Cuenta ****8663", "Tarjeta ** **5265".
        cuentas = re.findall(r"(?:[*•·]{2,}|x{2,})[\s*•·x]*(\d{4})\b", texto)

        return {
            "folio": folio,
            "monto": monto,
            "cedula_detectada": cedula_detectada,
            "fecha_pago": fecha_pago,
            "cuentas": sorted(set(cuentas)),
            "concepto": concepto,
            "huella": huella_captura(monto, fecha_pago, cuentas),
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
