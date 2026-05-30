import easyocr
import re
import httpx
import os
import logging

logger = logging.getLogger(__name__)

class OCRService:
    def __init__(self):
        # Cargamos el modelo en español e inglés
        self.reader = easyocr.Reader(['es', 'en'])

    async def procesar_ticket(self, url_imagen: str):
        """Descarga la imagen y extrae Folio, Monto y Cédula de Cliente (Concepto)"""
        temp_path = f"temp_ticket_{os.urandom(4).hex()}.jpg"
        
        try:
            # 1. Descargar la imagen
            async with httpx.AsyncClient() as client:
                resp = await client.get(url_imagen)
                with open(temp_path, "wb") as f:
                    f.write(resp.content)

            # 2. Leer texto
            resultados = self.reader.readtext(temp_path, detail=0)
            texto_crudo = " ".join(resultados)
            texto = texto_crudo.lower()
            
            logger.info(f"Texto OCR extraído: {texto}")

            # ==========================================
            # 3. EXTRACCIÓN DE FOLIO / REFERENCIA
            # ==========================================
            folio = None
            patrones_folio = [
                r"clave de rastreo[^\w]*([a-z0-9]{15,40})",
                r"rastreo es[^\w]*([a-z0-9]{15,40})",
                r"folio[^\w]*([a-z0-9]{6,40})",
                r"operaci[oó]n[^\w]*([a-z0-9]{6,40})",
                r"referencia[^\w]*([a-z0-9]{6,40})",
                r"ref\.[^\w]*([a-z0-9]{6,40})",
                r"autorizaci[oó]n[^\w]*([a-z0-9]{6,40})"
            ]

            for patron in patrones_folio:
                match = re.search(patron, texto)
                if match:
                    folio_encontrado = match.group(1).upper()
                    if not folio_encontrado.isalpha() and len(folio_encontrado) >= 6:
                        folio = folio_encontrado
                        break
            
            if not folio:
                palabras = texto_crudo.split()
                candidatos = [p.upper() for p in palabras if len(p) >= 10 and any(c.isdigit() for c in p)]
                if candidatos:
                    folio = max(candidatos, key=len)

            # ==========================================
            # 4. EXTRACCIÓN DE MONTO
            # ==========================================
            monto = 0.0
            patrones_monto = [
                r"[\$sS]\s?(\d{1,3}(?:,\d{3})*(?:\.\d{2}))",
                r"importe[^\d]*(\d{1,3}(?:,\d{3})*(?:\.\d{2}))",
                r"monto[^\d]*(\d{1,3}(?:,\d{3})*(?:\.\d{2}))",
                r"enviaste[^\d]*(\d{1,3}(?:,\d{3})*(?:\.\d{2}))",
                r"(\d{1,3}(?:,\d{3})*(?:\.\d{2}))\s?(mxn|mn|pesos)"
            ]

            for patron in patrones_monto:
                match = re.search(patron, texto)
                if match:
                    try:
                        monto = float(match.group(1).replace(",", ""))
                        if monto > 0:
                            break
                    except ValueError:
                        continue

            # ==========================================
            # 5. EXTRACCIÓN DE CÉDULA (CONCEPTO DE PAGO) 🚀
            # ==========================================
            cedula_detectada = None
            # Buscamos palabras como "concepto", "motivo", "mensaje", seguidas de un código alfanumérico corto (3 a 10 caracteres)
            patrones_concepto = [
                r"concepto[^\w]*([a-z0-9]{3,10})",
                r"motivo[^\w]*([a-z0-9]{3,10})",
                r"mensaje[^\w]*([a-z0-9]{3,10})",
                r"descripci[oó]n[^\w]*([a-z0-9]{3,10})"
            ]

            for patron in patrones_concepto:
                match = re.search(patron, texto)
                if match:
                    # Lo extraemos y lo pasamos a mayúsculas para que coincida con tu BD (Ej. "329B")
                    cedula_detectada = match.group(1).upper()
                    break

            return {
                "folio": folio,
                "monto": monto,
                "cedula_detectada": cedula_detectada, # 👈 Nuevo dato que regresa al bot
                "exito": folio is not None and monto > 0
            }

        except Exception as e:
            logger.error(f"Error procesando OCR: {e}")
            return {"folio": None, "monto": 0.0, "cedula_detectada": None, "exito": False}
            
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)