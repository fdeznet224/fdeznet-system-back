import asyncio
from datetime import datetime

import src.jobs as jobs


class _Resultado:
    def __init__(self, ids):
        self.ids = ids

    def scalars(self):
        return self

    def all(self):
        return self.ids


class _DB:
    def __init__(self, lotes):
        self.lotes = list(lotes)
        self.borrados = []
        self.commits = 0
        self.consultas = []

    async def execute(self, consulta):
        self.consultas.append(str(consulta))
        if str(consulta).startswith("DELETE"):
            self.borrados.append(consulta)
            return None
        return _Resultado(self.lotes.pop(0) if self.lotes else [])

    async def commit(self):
        self.commits += 1


def test_borra_por_lotes_hasta_que_no_queda_nada_viejo(monkeypatch):
    monkeypatch.setattr(jobs, "LOTE_BITACORA", 2)
    db = _DB([[1, 2], [3]])

    borrados = asyncio.run(jobs.limpiar_bitacora(db, ahora=datetime(2026, 10, 6)))

    assert borrados == 3
    assert len(db.borrados) == 2 and db.commits == 2
    assert "logs_cronjobs.fecha <" in db.consultas[0]


def test_sin_registros_viejos_no_borra_nada():
    db = _DB([])
    assert asyncio.run(jobs.limpiar_bitacora(db)) == 0
    assert db.borrados == []


def test_solo_borra_lecturas_automaticas_viejas():
    class _DBLecturas:
        def __init__(self):
            self.consulta = None

        async def execute(self, consulta):
            self.consulta = str(consulta)
            return type("R", (), {"rowcount": 3})()

        async def commit(self):
            return None

    db = _DBLecturas()
    assert asyncio.run(jobs.limpiar_lecturas_automaticas(db, ahora=datetime(2026, 10, 6))) == 3
    assert "DELETE FROM lecturas_opticas" in db.consulta and "lecturas_opticas.origen =" in db.consulta
