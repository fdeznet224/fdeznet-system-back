import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks
from sqlalchemy.dialects import mysql

from src.application.services.client_service import ClientService
from src.application.services.inventario_service import InventarioService
from src.domain.schemas import ClienteCreate


class _ScalarResult:
    def __init__(self, value=None, values=None):
        self.value = value
        self.values = values or []

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        return self.values


class _InventoryDB:
    def __init__(self):
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return _ScalarResult(values=[])


class _OccupiedOnuDB:
    def __init__(self):
        self.results = iter(
            [
                _ScalarResult(
                    value=SimpleNamespace(
                        id=2,
                        identificador="ONU-002",
                        estado="DISPONIBLE",
                    )
                ),
                _ScalarResult(
                    value=SimpleNamespace(
                        id=19,
                        nombre="Cliente existente",
                    )
                ),
            ]
        )
        self.rollbacks = 0

    async def execute(self, _statement):
        return next(self.results)

    async def rollback(self):
        self.rollbacks += 1


def test_disponibles_excluyen_onus_referenciadas():
    db = _InventoryDB()

    resultado = asyncio.run(
        InventarioService(db).obtener_equipos("DISPONIBLE")
    )

    consulta = str(
        db.statement.compile(
            dialect=mysql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert resultado == []
    assert "NOT (EXISTS (SELECT clientes.id" in consulta
    assert "NOT (EXISTS (SELECT servicios.id" in consulta


def test_alta_rechaza_onu_ocupada_antes_del_insert_y_hace_rollback():
    db = _OccupiedOnuDB()
    datos = ClienteCreate(nombre="Cliente nuevo", onu_id=2)

    with pytest.raises(ValueError, match="Cliente existente"):
        asyncio.run(
            ClientService(db).registrar_cliente(
                datos,
                BackgroundTasks(),
            )
        )

    assert db.rollbacks == 1


def test_alta_repetida_indica_usar_el_cliente_existente():
    existente = SimpleNamespace(id=13, nombre="Liz Perez Luna")

    class _DuplicateClientDB:
        async def execute(self, _statement):
            return _ScalarResult(value=existente)

    datos = ClienteCreate(
        nombre=" Liz Perez Luna ",
        telefono=" 9613632496 ",
    )

    with pytest.raises(
        ValueError,
        match="agrega un servicio al cliente existente",
    ):
        asyncio.run(
            ClientService(_DuplicateClientDB()).registrar_cliente(
                datos,
                BackgroundTasks(),
            )
        )


def test_migracion_concilia_onus_heredadas():
    migracion = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "e5f6a7b8c9d0_conciliar_onus_asignadas.py"
    ).read_text(encoding="utf-8")

    assert 'down_revision = "d4e5f6a7b8c9"' in migracion
    assert "SET i.estado = 'INSTALADO'" in migracion
    assert "SET i.estado = 'RESERVADO'" in migracion
    assert "FROM clientes AS c" in migracion
    assert "FROM servicios AS s" in migracion
