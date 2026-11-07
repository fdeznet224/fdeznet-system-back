"""La ONU de un cliente activo que dejó de reportarse se llama desde el ACS."""

import asyncio

import httpx

from src.application.services import despertar_tr069_service as despertar
from src.application.services.despertar_tr069_service import DespertarTr069Service


class DbFalsa:
    def __init__(self):
        self.logs = []

    def add(self, objeto):
        self.logs.append(objeto)

    async def commit(self):
        return None


def acs(contesta):
    tareas, borradas = [], []
    dormidos = [
        {"_id": "activa", "_lastInform": "2026-11-07T10:00:00.000Z", "_deviceId": {"_SerialNumber": "4857544308BD669F"}},
        {"_id": "suspendida", "_lastInform": "2026-11-07T10:00:00.000Z", "_deviceId": {"_SerialNumber": "485754434E35FEB2"}},
        {"_id": "bodega", "_lastInform": "2026-11-07T10:00:00.000Z", "_deviceId": {"_SerialNumber": "4857544345EB60A7"}},
    ]

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/devices":
            return httpx.Response(200, json=dormidos)
        if request.method == "POST":
            tareas.append(request.url.path)
            return httpx.Response(200 if contesta else 202, json={"_id": "t1"})
        if request.url.path == "/faults":
            return httpx.Response(200, json=[])
        if request.url.path == "/tasks":
            return httpx.Response(200, json=[{"_id": "t1", "name": "getParameterValues"}])
        if request.method == "DELETE":
            borradas.append(request.url.path)
            return httpx.Response(200)
        return httpx.Response(404)

    return responder, tareas, borradas


def correr(contesta, monkeypatch):
    despertar._ultimo_llamado.clear()
    responder, tareas, borradas = acs(contesta)
    db = DbFalsa()
    servicio = DespertarTr069Service(db, nbi="http://nbi", transporte=httpx.MockTransport(responder))

    async def mapas(_db):
        return ({"4857544308BD669F": {"id": 13, "nombre": "Rutila"}, "485754434E35FEB2": {"id": 21, "nombre": "Ivan"}}, {})

    async def activos():
        return {13}

    monkeypatch.setattr(despertar, "clientes_por_equipo", mapas)
    monkeypatch.setattr(servicio, "_activos", activos)
    conteo = asyncio.run(servicio.revisar())
    return conteo, tareas, borradas, db


def test_llama_solo_a_la_de_cliente_activo_y_si_contesta_lo_anota(monkeypatch):
    conteo, tareas, borradas, db = correr(True, monkeypatch)
    assert conteo == {"llamados": 1, "contestaron": 1}
    assert tareas == ["/devices/activa/tasks"] and borradas == []
    assert "contestó" in db.logs[0].mensaje and "Rutila" in db.logs[0].mensaje


def test_si_no_contesta_quita_la_orden_y_no_insiste_en_la_hora(monkeypatch):
    conteo, tareas, borradas, db = correr(False, monkeypatch)
    assert conteo == {"llamados": 1, "contestaron": 0} and borradas == ["/tasks/t1"]
    assert db.logs[0].nivel == "WARN"
    # Otra vuelta enseguida: no se le vuelve a llamar.
    responder, tareas2, _ = acs(False)
    servicio = DespertarTr069Service(DbFalsa(), nbi="http://nbi", transporte=httpx.MockTransport(responder))

    async def activos():
        return {13}

    monkeypatch.setattr(servicio, "_activos", activos)
    assert asyncio.run(servicio.revisar()) == {"llamados": 0, "contestaron": 0} and tareas2 == []
