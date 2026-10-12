"""ONU en línea sin PPPoE: aviso tras 15 minutos y reautorización en la OLT."""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from src.application.services import onu_sin_pppoe_service as modulo
from src.application.services.onu_sin_pppoe_service import (
    OnuSinPppoeService,
    mensaje_aviso,
    pendientes_de_aviso,
    seguir_reautorizacion,
)

AHORA = datetime(2026, 10, 11, 20, 0)
RUBI = {"cliente_id": 254, "nombre": "Rubi Fernandez", "olt": "Vicente Guerrero",
        "ubicacion": "GPON0/1:2", "serial": "HWTCAEC3C0AD", "rx": "-20.27"}


def test_avisa_solo_despues_de_15_minutos_y_una_vez():
    vistos, avisados = {}, set()
    assert pendientes_de_aviso({254: RUBI}, vistos, avisados, AHORA) == []
    assert pendientes_de_aviso({254: RUBI}, vistos, avisados, AHORA + timedelta(minutes=10)) == []
    assert pendientes_de_aviso({254: RUBI}, vistos, avisados, AHORA + timedelta(minutes=15)) == [RUBI]
    assert pendientes_de_aviso({254: RUBI}, vistos, avisados, AHORA + timedelta(minutes=30)) == []


def test_si_se_arregla_y_vuelve_a_pasar_se_avisa_de_nuevo():
    vistos, avisados = {}, set()
    pendientes_de_aviso({254: RUBI}, vistos, avisados, AHORA)
    pendientes_de_aviso({254: RUBI}, vistos, avisados, AHORA + timedelta(minutes=15))
    pendientes_de_aviso({}, vistos, avisados, AHORA + timedelta(minutes=30))
    assert vistos == {} and avisados == set()
    pendientes_de_aviso({254: RUBI}, vistos, avisados, AHORA + timedelta(hours=2))
    assert pendientes_de_aviso({254: RUBI}, vistos, avisados, AHORA + timedelta(hours=2, minutes=15)) == [RUBI]


def test_el_mensaje_dice_quien_donde_y_que_hacer():
    texto = mensaje_aviso([RUBI])
    assert "Rubi Fernandez · Vicente Guerrero GPON0/1:2 · -20.27 dBm" in texto
    assert "Reautorizar en la OLT" in texto


OLT = SimpleNamespace(id=7, nombre="Vicente Guerrero", tipo_integracion="vsol_api", api_enabled=True)


def _cliente(**kw):
    datos = dict(id=254, nombre="Rubi Fernandez", cedula="29A0", estado="activo", user_pppoe="Rubi_F",
                 router_id=2, olt_id=7, onu_asignada=SimpleNamespace(identificador="HWTCAEC3C0AD"))
    datos.update(kw)
    return SimpleNamespace(**datos)


class _VsolFalsa:
    def __init__(self, listados):
        self.listados = list(listados)
        self.borradas = []
        self.descripciones = []

    async def listar_onus_unificadas(self, olt_id):
        return {"onus": self.listados.pop(0) if len(self.listados) > 1 else self.listados[0]}

    def _eliminar_onu_sync(self, olt, pon, onuid, serial):
        self.borradas.append((pon, onuid, serial))
        return {"eliminada": True}

    def _escribir_descripciones_sync(self, olt, cambios):
        self.descripciones += cambios
        return {"escritas": len(cambios)}


def _onu(estado="online", ubicacion="GPON0/1:2", serial="HWTCAEC3C0AD"):
    return {"onu_id": ubicacion, "pon_id": ubicacion.split("/")[1].split(":")[0], "identificador": serial,
            "estado_fisico": estado, "rx_power": "-20.27"}


class _DB:
    def __init__(self, cliente=None):
        self.cliente = cliente
        self.agregados = []
        self.commits = 0

    async def execute(self, _consulta):
        cliente = self.cliente
        return SimpleNamespace(scalar_one_or_none=lambda: cliente,
                               scalars=lambda: SimpleNamespace(all=lambda: [OLT]))

    async def get(self, modelo, _llave):
        return OLT if modelo.__name__ == "OLTModel" else self.cliente

    def add(self, objeto):
        self.agregados.append(objeto)

    async def commit(self):
        self.commits += 1


def _detectar(onus, cliente, activos):
    servicio = OnuSinPppoeService(_DB(), _VsolFalsa([onus]))

    async def clientes(_self):
        return {"HWTCAEC3C0AD": cliente}

    async def sesiones(_router_id, _cache):
        return activos

    servicio._sesiones = sesiones
    original = modulo.DescripcionOnuService._clientes_por_onu
    modulo.DescripcionOnuService._clientes_por_onu = clientes
    try:
        return asyncio.run(servicio.detectar())
    finally:
        modulo.DescripcionOnuService._clientes_por_onu = original


def test_detecta_onu_en_linea_sin_sesion():
    detectados = _detectar([_onu()], _cliente(), {"Otro"})
    assert detectados[254]["ubicacion"] == "GPON0/1:2" and detectados[254]["rx"] == "-20.27"


@pytest.mark.parametrize("onus, cliente, activos", [
    ([_onu()], _cliente(), {"Rubi_F"}),                  # tiene sesión
    ([_onu(estado="offline")], _cliente(), {"Otro"}),     # la ONU está apagada: es otro problema
    ([_onu()], _cliente(estado="suspendido"), {"Otro"}),  # cortado por pago
    ([_onu()], _cliente(), None),                         # el MikroTik no respondió
])
def test_no_avisa_lo_que_no_es_este_caso(onus, cliente, activos):
    assert _detectar(onus, cliente, activos) == {}


def test_reautorizar_borra_la_onu_en_linea_y_sigue_en_segundo_plano(monkeypatch):
    lanzadas = []
    monkeypatch.setattr(modulo, "seguir_reautorizacion", lambda *a: lanzadas.append(a) or asyncio.sleep(0))
    vsol = _VsolFalsa([[_onu()]])
    db = _DB(_cliente())
    resultado = asyncio.run(OnuSinPppoeService(db, vsol).reautorizar(254, SimpleNamespace(id=1, usuario="admin")))
    assert vsol.borradas == [(1, 2, "HWTCAEC3C0AD")]
    assert resultado["ubicacion_anterior"] == "GPON0/1:2"
    assert lanzadas == [(254, 7, "HWTCAEC3C0AD")]
    assert db.commits == 1 and "reautorizar-onu" in db.agregados[0].ruta


def test_no_reautoriza_una_onu_apagada():
    vsol = _VsolFalsa([[_onu(estado="offline")]])
    with pytest.raises(ValueError, match="apagada"):
        asyncio.run(OnuSinPppoeService(_DB(_cliente()), vsol).reautorizar(254))
    assert vsol.borradas == []


def test_el_seguimiento_pone_el_nombre_y_confirma_el_pppoe(monkeypatch):
    # Primero no aparece; luego vuelve en otro número.
    vsol = _VsolFalsa([[], [_onu(ubicacion="GPON0/1:1")]])
    class _Vsol(modulo.VsolApiService):
        def __new__(cls, _db):
            return vsol

    monkeypatch.setattr(modulo, "VsolApiService", _Vsol)

    class _MK:
        def __init__(self, *a):
            self.intentos = 0

        def obtener_info_sesion(self, usuario):
            self.intentos += 1
            return {"online": self.intentos > 1, "ip": "10.10.10.30"}

    monkeypatch.setattr(modulo, "MikroTikService", _MK)
    db = _DB(_cliente())
    router = SimpleNamespace(ip_vpn="1", user_api="u", pass_api="p", port_api=80)
    db.get = lambda modelo, _llave: asyncio.sleep(0, {"OLTModel": OLT, "RouterModel": router}.get(modelo.__name__, db.cliente))

    class _Sesion:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *a):
            return False

    async def sin_esperar(_s):
        return None

    mensaje = asyncio.run(seguir_reautorizacion(254, 7, "HWTCAEC3C0AD", dormir=sin_esperar, sesiones=_Sesion))
    assert vsol.descripciones == [(1, 1, "HWTCAEC3C0AD", "Rubi Fernandez 29A0")]
    assert "GPON0/1:1 y PPPoE conectado (10.10.10.30)" in mensaje
    assert db.agregados[-1].origen == "ReautorizarONU" and db.agregados[-1].nivel == "INFO"
