import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from src.domain.schemas import BootstrapExchangeRequest
from src.interfaces.api import control_plane


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _DB:
    def __init__(self, installation, release):
        self.results = [installation, release]
        self.committed = False

    async def execute(self, _query):
        return _Result(self.results.pop(0))

    async def commit(self):
        self.committed = True


def test_bootstrap_entrega_una_version_completa_y_fija(monkeypatch):
    installation = SimpleNamespace(
        instalacion_id="00000000-0000-0000-0000-000000000001",
        nombre_isp="ISP Piloto",
        dominio="panel.piloto.test",
        contacto_email="admin@piloto.test",
        licencia_hash="0" * 64,
        bootstrap_usado_en=None,
        bootstrap_expira=(
            datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
        ),
        estado="activa",
        version_objetivo=None,
    )
    release = SimpleNamespace(
        version="2.7.0",
        backend_commit="a" * 40,
        frontend_commit="b" * 40,
    )
    db = _DB(installation, release)
    monkeypatch.setattr(control_plane, "_ensure_central", lambda: None)

    response = asyncio.run(
        control_plane.exchange_bootstrap(
            BootstrapExchangeRequest(token="x" * 40),
            db=db,
        )
    )

    assert response.version == "2.7.0"
    assert response.backend_commit == "a" * 40
    assert response.frontend_commit == "b" * 40
    assert installation.version_objetivo == "2.7.0"
    assert installation.bootstrap_usado_en is not None
    assert db.committed is True
