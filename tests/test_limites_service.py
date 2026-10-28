import asyncio

import pytest
from fastapi import HTTPException

from src.application.services import limites_service


class _DB:
    def __init__(self, total):
        self.total = total

    async def scalar(self, _consulta):
        return self.total


def test_sin_limite_no_bloquea(monkeypatch):
    monkeypatch.delenv("FDEZNET_LIMITE_CLIENTES", raising=False)
    monkeypatch.delenv("FDEZNET_LIMITE_ROUTERS", raising=False)
    assert asyncio.run(limites_service.remaining_client_capacity(_DB(5000))) is None
    asyncio.run(limites_service.ensure_client_capacity(_DB(5000)))
    asyncio.run(limites_service.ensure_router_capacity(_DB(50)))


def test_limite_de_abonados_del_plan(monkeypatch):
    monkeypatch.setenv("FDEZNET_LIMITE_CLIENTES", "300")
    assert asyncio.run(limites_service.remaining_client_capacity(_DB(280))) == 20
    with pytest.raises(HTTPException) as error:
        asyncio.run(limites_service.ensure_client_capacity(_DB(300)))
    assert error.value.status_code == 403


def test_limite_de_routers_del_plan(monkeypatch):
    monkeypatch.setenv("FDEZNET_LIMITE_ROUTERS", "2")
    asyncio.run(limites_service.ensure_router_capacity(_DB(1)))
    with pytest.raises(HTTPException):
        asyncio.run(limites_service.ensure_router_capacity(_DB(2)))
