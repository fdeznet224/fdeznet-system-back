import asyncio
from datetime import date, datetime
from types import SimpleNamespace

from src.application.services import agenda_service as agenda
from src.application.services.agenda_service import bloques_del_dia, preparar_aviso_en_camino


def test_la_primera_visita_es_media_hora_despues_de_abrir_y_cada_una_dura_dos_horas():
    assert bloques_del_dia({"activo": True, "inicio": "08:00", "fin": "20:00"}) == ["08:30", "10:30", "12:30", "14:30", "16:30"]
    assert bloques_del_dia({"activo": True, "inicio": "09:00", "fin": "14:00"}) == ["09:30", "11:30"]
    assert bloques_del_dia({"activo": False, "inicio": "08:00", "fin": "20:00"}) == []


class _Resultado:
    def __init__(self, valor):
        self.valor = valor

    def scalars(self):
        return self

    def all(self):
        return self.valor

    def first(self):
        return self.valor

    def scalar_one_or_none(self):
        return self.valor


def test_la_agenda_marca_los_bloques_ocupados_y_el_siguiente_libre(monkeypatch):
    visita = SimpleNamespace(id=7, cliente_id=None, prospecto_nombre="Ana", fecha_programada=datetime(2026, 10, 5, 8, 30))

    class _DB:
        async def execute(self, _consulta):
            return _Resultado([visita])

        async def get(self, *_):
            return None

    async def horario(_db, _dia):
        return {"activo": True, "inicio": "08:00", "fin": "14:00"}

    monkeypatch.setattr(agenda, "horario_de", horario)
    resultado = asyncio.run(agenda.agenda_del_dia(_DB(), 3, date(2026, 10, 5)))

    assert [b["hora"] for b in resultado["bloques"]] == ["08:30", "10:30"]
    assert resultado["bloques"][0] == {"hora": "08:30", "orden_id": 7, "nombre": "Ana"}
    assert resultado["siguiente_libre"] == "10:30"


def test_el_aviso_en_camino_va_al_chat_del_prospecto_una_sola_vez():
    agregados = []
    ya_enviado = {"valor": None}

    class _DB:
        async def execute(self, _consulta):
            return _Resultado(ya_enviado["valor"])

        async def get(self, modelo, _ident):
            return SimpleNamespace(empresa_nombre="FdezNet") if modelo.__name__ == "ConfiguracionSistema" else None

        def add(self, objeto):
            agregados.append(objeto)

    orden = SimpleNamespace(id=41, cliente_id=None, prospecto_nombre="Ana Lopez", prospecto_telefono="9611234567",
                            descripcion="Chat: 82769002647735@lid")
    tecnico = SimpleNamespace(nombre_completo="Pedro Gomez", usuario="pedro")

    mensaje = asyncio.run(preparar_aviso_en_camino(_DB(), orden, tecnico))

    assert mensaje.telefono == "82769002647735@lid"
    assert mensaje.mensaje.startswith("👷 Hola Ana, el técnico Pedro de FdezNet ya va en camino")
    assert mensaje.clave_dedupe == "orden:41:en_camino" and mensaje.estado_envio == "pendiente"

    ya_enviado["valor"] = 99
    assert asyncio.run(preparar_aviso_en_camino(_DB(), orden, tecnico)) is None
    assert len(agregados) == 1
