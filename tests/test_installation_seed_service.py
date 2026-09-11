import asyncio
from types import SimpleNamespace

from src.application.services.installation_seed_service import seed_fresh_installation


class _Scalars:
    def __init__(self, values):
        self.values = values

    def all(self):
        return self.values


class FakeDB:
    def __init__(self):
        self.added = []
        self.commits = 0

    async def get(self, _model, _identifier):
        return None

    async def scalar(self, _statement):
        return None

    async def scalars(self, _statement):
        return _Scalars([])

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.commits += 1


def test_fresh_seed_creates_bot_flows_template_and_license_plans():
    database = FakeDB()
    asyncio.run(seed_fresh_installation(database))

    tables = [item.__tablename__ for item in database.added]
    assert tables.count("configuracion_bot") == 1
    assert tables.count("flujos_bot") == 2
    assert tables.count("plantillas_mensajes") == 1
    assert tables.count("planes_licencia") == 4
    assert database.commits == 1
