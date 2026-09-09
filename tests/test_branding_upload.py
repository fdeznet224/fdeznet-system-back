import asyncio
import io
import tempfile
from types import SimpleNamespace

from fastapi import UploadFile
from PIL import Image

from src.interfaces.api import configuracion


class _DB:
    async def commit(self):
        return None

    async def refresh(self, _config):
        return None


def test_favicon_genera_iconos_pwa_cuadrados(monkeypatch, tmp_path):
    config = SimpleNamespace(favicon_url=None)

    async def obtener_configuracion(_db):
        return config

    async def clear_cache():
        return None

    contenido = io.BytesIO()
    Image.new("RGB", (900, 300), "#2457d6").save(contenido, format="PNG")
    archivo_temporal = tempfile.SpooledTemporaryFile(max_size=2 * 1024 * 1024)
    archivo_temporal.write(contenido.getvalue())
    archivo_temporal.seek(0)
    archivo = UploadFile(filename="marca.png", file=archivo_temporal)
    monkeypatch.setattr(configuracion, "BRANDING_DIR", tmp_path)
    monkeypatch.setattr(configuracion, "_obtener_configuracion", obtener_configuracion)
    monkeypatch.setattr(configuracion.FastAPICache, "clear", clear_cache)

    resultado = asyncio.run(
        configuracion.subir_archivo_marca("favicon", archivo, db=_DB())
    )

    assert Image.open(tmp_path / "favicon.png").size == (512, 512)
    assert Image.open(tmp_path / "favicon-192.png").size == (192, 192)
    assert resultado.favicon_url.startswith(
        "/api/public/marca/archivo/favicon?v="
    )
