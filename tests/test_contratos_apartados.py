"""Contratos apartados: el técnico sabe el número antes de instalar."""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import src.application.services.contrato_service as modulo
from src.application.services.contrato_service import (
    APARTADOS_POR_TECNICO,
    ContratoService,
    codigo_disponible,
    generar_codigo_contrato,
)


class _Lista:
    def __init__(self, valores):
        self.valores = valores

    def scalars(self):
        return self

    def all(self):
        return self.valores


class _DB:
    """Simula clientes y apartados; responde a las consultas del servicio."""

    def __init__(self, clientes=(), apartados=()):
        self.clientes = set(clientes)
        self.apartados = list(apartados)
        self.commits = 0

    def _vigentes(self, usuario_id):
        return [a for a in self.apartados if a.usuario_id == usuario_id and not a.usado_en and not a.liberado_en]

    async def execute(self, consulta):
        usuario_id = consulta.compile().params["usuario_id_1"]
        # Igual que la consulta real: por fecha y luego por orden de creación.
        return _Lista(sorted(self._vigentes(usuario_id), key=lambda a: a.reservado_en))

    async def scalar(self, consulta):
        params = consulta.compile().params
        codigo = params.get("cedula_1") or params.get("codigo_1")
        if "cedula_1" in params:
            return 1 if codigo in self.clientes else None
        encontrados = [a for a in self.apartados if a.codigo == codigo]
        if "usuario_id_1" in params:
            encontrados = [a for a in encontrados if a.usuario_id == params["usuario_id_1"]]
        if len(consulta.selected_columns) == 1:  # ¿existe el código? (solo el id)
            return 1 if encontrados else None
        return encontrados[0] if encontrados else None

    def add(self, objeto):
        self.apartados.append(objeto)

    async def flush(self):
        return None

    async def commit(self):
        self.commits += 1


def _apartado(codigo, usuario_id=10, dias=0, **extra):
    datos = dict(codigo=codigo, usuario_id=usuario_id, reservado_en=datetime.now() - timedelta(days=dias),
                 usado_en=None, liberado_en=None, cliente_id=None)
    datos.update(extra)
    return SimpleNamespace(**datos)


def test_el_tecnico_siempre_tiene_cinco_apartados_unicos():
    db = _DB()
    apartados = asyncio.run(ContratoService(db).apartados(10))
    codigos = [a.codigo for a in apartados]
    assert len(codigos) == APARTADOS_POR_TECNICO == 5
    assert len(set(codigos)) == 5
    assert all(len(c) == 4 and set(c) <= set("0123456789ABCDEF") for c in codigos)
    # Pedirlos otra vez no crea más: son los mismos.
    assert [a.codigo for a in asyncio.run(ContratoService(db).apartados(10))] == codigos


def test_el_codigo_nuevo_no_choca_con_clientes_ni_con_apartados(monkeypatch):
    db = _DB(clientes={"BD0F"}, apartados=[_apartado("A7F2", usuario_id=99)])
    salidas = iter(["BD0F", "A7F2", "C31B"])
    monkeypatch.setattr(modulo.random, "choices", lambda *_a, **_k: list(next(salidas)))
    assert asyncio.run(generar_codigo_contrato(db)) == "C31B"
    assert asyncio.run(codigo_disponible(db, "A7F2")) is False


def test_un_apartado_vencido_se_libera_y_se_repone():
    viejo = _apartado("0AAA", dias=31)
    db = _DB(apartados=[viejo])
    apartados = asyncio.run(ContratoService(db).apartados(10))
    assert viejo.liberado_en is not None
    assert "0AAA" not in [a.codigo for a in apartados] and len(apartados) == 5


TECNICO = SimpleNamespace(id=10, rol="tecnico")
ADMIN = SimpleNamespace(id=1, rol="admin")


def test_usar_un_apartado_lo_liga_al_cliente():
    apartado = _apartado("A7F2")
    db = _DB(apartados=[apartado])
    assert asyncio.run(ContratoService(db).usar(TECNICO, "a7f2", 300)) == "A7F2"
    assert apartado.usado_en is not None and apartado.cliente_id == 300


@pytest.mark.parametrize("apartado, mensaje", [
    (_apartado("A7F2", usuario_id=11), "no está apartado a tu nombre"),
    (_apartado("A7F2", usado_en=datetime.now()), "ya se usó"),
    (_apartado("A7F2", liberado_en=datetime.now()), "ya fue liberado"),
])
def test_no_se_usa_un_apartado_ajeno_usado_o_liberado(apartado, mensaje):
    with pytest.raises(ValueError, match=mensaje):
        asyncio.run(ContratoService(_DB(apartados=[apartado])).usar(TECNICO, "A7F2", 300))


def test_descartar_libera_el_apartado():
    apartado = _apartado("A7F2")
    asyncio.run(ContratoService(_DB(apartados=[apartado])).descartar(10, "A7F2"))
    assert apartado.liberado_en is not None


def test_el_admin_da_de_alta_con_el_contrato_que_aparto_un_tecnico():
    apartado = _apartado("A7F2", usuario_id=10)
    assert asyncio.run(ContratoService(_DB(apartados=[apartado])).usar(ADMIN, "A7F2", 301)) == "A7F2"
    assert apartado.cliente_id == 301


def test_el_admin_no_puede_usar_un_codigo_que_nadie_aparto():
    with pytest.raises(ValueError, match="no está apartado a ningún técnico"):
        asyncio.run(ContratoService(_DB()).usar(ADMIN, "FFFF", 301))
