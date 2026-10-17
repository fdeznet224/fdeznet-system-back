import asyncio
from types import SimpleNamespace

import pytest

from src.application.services.ftth_service import FTTHService
from src.infrastructure.models import ClienteModel, ServicioModel


class _Resultado:
    def __init__(self, valores):
        self.valores = valores

    def scalars(self):
        return self

    def all(self):
        return self.valores

    def __iter__(self):
        return iter(self.valores)

    def scalar_one_or_none(self):
        return self.valores[0] if self.valores else None


class _DB:
    """Responde en orden: registro del puerto, socio, contratos del destino,
    registros a vaciar."""

    def __init__(self, socio=None, ajenos=(), registro=None, capacidad=8):
        self.caja = SimpleNamespace(id=1, capacidad=capacidad)
        self.respuestas = iter(
            [
                _Resultado([registro] if registro else []),
                _Resultado([socio] if socio else []),
                _Resultado(list(ajenos)),
                _Resultado([]),
            ]
        )
        self.flushes = 0

    async def get(self, _modelo, _id):
        return self.caja

    async def execute(self, _sentencia):
        return next(self.respuestas)

    async def flush(self):
        self.flushes += 1


def _cliente(id, puerto, caja=1):
    return ClienteModel(id=id, nombre=f"Cliente {id}", caja_nap_id=caja, puerto_nap=puerto)


def _servicio(id, cliente, puerto, caja=1, estado="activo"):
    return ServicioModel(
        id=id, cliente_id=cliente.id, caja_nap_id=caja, puerto_nap=puerto, estado=estado
    )


def _mover(db, cliente, servicios, caja, puerto, monkeypatch):
    sincronizadas = []

    async def sincronizar(_self, caja_id):
        sincronizadas.append(caja_id)

    monkeypatch.setattr(FTTHService, "sincronizar_puertos_nap", sincronizar)
    asyncio.run(FTTHService(db).mover_puerto_desde_ficha(cliente, servicios, caja, puerto))
    return sincronizadas


def test_mover_a_puerto_libre_lleva_tambien_el_contrato(monkeypatch):
    cliente = _cliente(1, 3)
    servicio = _servicio(10, cliente, 3)

    cajas = _mover(_DB(), cliente, [servicio], 1, 5, monkeypatch)

    assert (cliente.caja_nap_id, cliente.puerto_nap) == (1, 5)
    assert (servicio.caja_nap_id, servicio.puerto_nap) == (1, 5)
    assert cajas == [1]


def test_puerto_ocupado_de_la_misma_caja_intercambia_clientes(monkeypatch):
    cliente = _cliente(1, 3)
    servicio = _servicio(10, cliente, 3)
    socio = _cliente(2, 5)
    servicio_socio = _servicio(20, socio, 5)

    _mover(
        _DB(socio=socio, ajenos=[servicio_socio]),
        cliente, [servicio], 1, 5, monkeypatch,
    )

    assert (cliente.puerto_nap, servicio.puerto_nap) == (5, 5)
    assert (socio.puerto_nap, servicio_socio.puerto_nap) == (3, 3)


def test_contrato_desfasado_se_alinea_con_la_ficha(monkeypatch):
    # La ficha ya estaba en el 3 pero el contrato se quedó en el 7.
    cliente = _cliente(1, 3)
    servicio = _servicio(10, cliente, 7)

    _mover(_DB(), cliente, [servicio], 1, 4, monkeypatch)

    assert servicio.puerto_nap == 4


def test_contrato_fantasma_en_el_destino_vuelve_con_su_cliente(monkeypatch):
    cliente = _cliente(1, 3)
    otro = _cliente(2, 6)
    fantasma = _servicio(20, otro, 5)
    db = _DB(ajenos=[fantasma])

    async def get(modelo, _id):
        return otro if modelo is ClienteModel else db.caja

    db.get = get
    _mover(db, cliente, [], 1, 5, monkeypatch)

    assert cliente.puerto_nap == 5
    assert fantasma.puerto_nap == 6


def test_no_intercambia_con_cliente_de_otra_caja(monkeypatch):
    cliente = _cliente(1, 3, caja=2)
    socio = _cliente(2, 5)

    with pytest.raises(ValueError, match="misma caja"):
        _mover(_DB(socio=socio), cliente, [], 1, 5, monkeypatch)


def test_puerto_danado_no_se_asigna(monkeypatch):
    cliente = _cliente(1, 3)

    with pytest.raises(ValueError, match="dañado"):
        _mover(
            _DB(registro=SimpleNamespace(estado="danado")),
            cliente, [], 1, 5, monkeypatch,
        )
