import easyocr
import re
import httpx
import os

class OCRService:
    def __init__(self):
        # Cargamos el modelo en español (esto tarda un poco la primera vez)
        self.reader = easyocr.Reader(['es'])

    async def procesar_ticket(self, url_imagen: str):
        """Descarga la imagen y extrae Folio y Monto"""
        temp_path = "temp_ticket.jpg"
        
        # 1. Descargar la imagen desde el puente de Node.js
        async with httpx.AsyncClient() as client:
            resp = await client.get(url_imagen)
            with open(temp_path, "wb") as f:
                f.write(resp.content)

        # 2. Leer texto
        resultados = self.reader.readtext(temp_path, detail=0)
        texto = " ".join(resultados).lower()
        
        # Borrar archivo temporal
        if os.path.exists(temp_path): os.remove(temp_path)

        # 3. Buscar Folio (Patrones de Banco Azteca / SPEI)
        # Buscamos números largos de 10 a 20 dígitos cerca de la palabra 'folio' o 'rastreo'
        patron_folio = r"(folio|rastreo|referencia|autorizaci[oó]n)[\s#:-]+([a-z0-9]{8,25})"
        match_folio = re.search(patron_folio, texto)
        folio = match_folio.group(2).upper() if match_folio else None

        # 4. Buscar Monto
        patron_monto = r"\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)"
        match_monto = re.search(patron_monto, texto)
        monto = float(match_monto.group(1).replace(",", "")) if match_monto else 0.0

        return {
            "folio": folio,
            "monto": monto,
            "exito": folio is not None and monto > 0
        }